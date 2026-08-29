#!/usr/bin/env python3
"""
Oracle Cloud A1.Flex auto-creator.
Runs every 5 min via GitHub Actions (free, no Claude usage).
Exits 0 = out of capacity (retry next run).
Exits 1 = SUCCESS (triggers GitHub email notification to you).
"""
 
import oci
import os
import sys
import time
from datetime import datetime
 
def log(msg):
    print(f"[{datetime.utcnow().strftime('%H:%M:%S UTC')}] {msg}", flush=True)
 
COMPARTMENT_ID = os.environ['OCI_COMPARTMENT_ID']
SSH_KEY        = os.environ['SSH_PUBLIC_KEY']
DISPLAY_NAME   = 'minecraft-server'
SHAPE          = 'VM.Standard.A1.Flex'
OCPUS          = 2
MEMORY_GB      = 12
BOOT_GB        = 50
 
config   = oci.config.from_file()
compute  = oci.core.ComputeClient(config)
network  = oci.core.VirtualNetworkClient(config)
identity = oci.identity.IdentityClient(config)
 
# ── Auto-discover resources ───────────────────────────────────────────────────
 
def get_availability_domain():
    ads = identity.list_availability_domains(compartment_id=COMPARTMENT_ID).data
    return ads[0].name  # Sydney only has AD-1
 
def get_ubuntu_arm_image():
    images = compute.list_images(
        compartment_id=COMPARTMENT_ID,
        operating_system='Canonical Ubuntu',
        operating_system_version='22.04',
        shape=SHAPE,
        sort_by='TIMECREATED',
        sort_order='DESC'
    ).data
    if not images:
        raise Exception("No Ubuntu 22.04 ARM image found in this region")
    return images[0].id
 
def get_or_create_subnet():
    """Find an available subnet, or create a full VCN + subnet from scratch."""
 
    # Look for existing available VCNs
    vcns = [v for v in network.list_vcns(compartment_id=COMPARTMENT_ID).data
            if v.lifecycle_state == 'AVAILABLE']
 
    for vcn in vcns:
        subnets = [s for s in network.list_subnets(
                       compartment_id=COMPARTMENT_ID, vcn_id=vcn.id).data
                   if s.lifecycle_state == 'AVAILABLE']
        if subnets:
            log(f"Found existing subnet: {subnets[0].id}")
            return subnets[0].id
 
    # Nothing found — create VCN + internet gateway + subnet
    log("No available subnet found. Creating VCN + subnet...")
 
    # 1. Create VCN
    vcn = network.create_vcn(oci.core.models.CreateVcnDetails(
        compartment_id=COMPARTMENT_ID,
        display_name='minecraft-vcn',
        cidr_block='10.0.0.0/16'
    )).data
    log(f"VCN created: {vcn.id}")
 
    # 2. Wait for VCN
    for _ in range(12):
        vcn = network.get_vcn(vcn.id).data
        if vcn.lifecycle_state == 'AVAILABLE':
            break
        time.sleep(5)
 
    # 3. Create Internet Gateway
    ig = network.create_internet_gateway(oci.core.models.CreateInternetGatewayDetails(
        compartment_id=COMPARTMENT_ID,
        vcn_id=vcn.id,
        display_name='minecraft-ig',
        is_enabled=True
    )).data
    log(f"Internet gateway created: {ig.id}")
    time.sleep(3)
 
    # 4. Add default route via IG
    rts = network.list_route_tables(compartment_id=COMPARTMENT_ID, vcn_id=vcn.id).data
    if rts:
        network.update_route_table(rts[0].id, oci.core.models.UpdateRouteTableDetails(
            route_rules=[oci.core.models.RouteRule(
                network_entity_id=ig.id,
                destination='0.0.0.0/0',
                destination_type='CIDR_BLOCK'
            )]
        ))
        log("Default route added to route table.")
 
    # 5. Open security list ports: 22 (SSH), 25565 (Minecraft), 3000 (Dashboard)
    sls = network.list_security_lists(compartment_id=COMPARTMENT_ID, vcn_id=vcn.id).data
    if sls:
        ingress = sls[0].ingress_security_rules or []
        needed_ports = [22, 25565, 3000]
        existing_ports = {r.tcp_options.destination_port_range.min
                         for r in ingress
                         if r.tcp_options and r.tcp_options.destination_port_range}
        new_rules = list(ingress)
        for port in needed_ports:
            if port not in existing_ports:
                new_rules.append(oci.core.models.IngressSecurityRule(
                    protocol='6',  # TCP
                    source='0.0.0.0/0',
                    source_type='CIDR_BLOCK',
                    tcp_options=oci.core.models.TcpOptions(
                        destination_port_range=oci.core.models.PortRange(min=port, max=port)
                    )
                ))
        network.update_security_list(sls[0].id, oci.core.models.UpdateSecurityListDetails(
            ingress_security_rules=new_rules,
            egress_security_rules=sls[0].egress_security_rules
        ))
        log(f"Security list updated with ports {needed_ports}.")
 
    # 6. Create subnet
    subnet = network.create_subnet(oci.core.models.CreateSubnetDetails(
        compartment_id=COMPARTMENT_ID,
        vcn_id=vcn.id,
        display_name='minecraft-subnet',
        cidr_block='10.0.0.0/24'
    )).data
    log(f"Subnet created: {subnet.id}")
 
    # Wait for subnet
    for _ in range(12):
        subnet = network.get_subnet(subnet.id).data
        if subnet.lifecycle_state == 'AVAILABLE':
            break
        time.sleep(5)
 
    return subnet.id
 
def instance_exists():
    instances = compute.list_instances(
        compartment_id=COMPARTMENT_ID,
        display_name=DISPLAY_NAME
    ).data
    active = [i for i in instances if i.lifecycle_state not in ('TERMINATED', 'TERMINATING')]
    return active[0] if active else None
 
# ── Main ──────────────────────────────────────────────────────────────────────
 
def main():
    log("Checking if instance already exists...")
    existing = instance_exists()
    if existing:
        log(f"Instance already running! ID: {existing.id}")
        log(f"State: {existing.lifecycle_state}")
        log("SUCCESS — nothing to do.")
        sys.exit(1)  # exit 1 triggers GitHub email notification
 
    log("No instance found. Discovering resources...")
    ad        = get_availability_domain()
    image_id  = get_ubuntu_arm_image()
    subnet_id = get_or_create_subnet()
    log(f"AD: {ad}")
    log(f"Image: {image_id}")
    log(f"Subnet: {subnet_id}")
 
    log(f"Attempting to create {SHAPE} ({OCPUS} OCPU / {MEMORY_GB}GB RAM)...")
    try:
        result = compute.launch_instance(
            oci.core.models.LaunchInstanceDetails(
                compartment_id      = COMPARTMENT_ID,
                availability_domain = ad,
                display_name        = DISPLAY_NAME,
                shape               = SHAPE,
                shape_config        = oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=OCPUS, memory_in_gbs=MEMORY_GB
                ),
                source_details      = oci.core.models.InstanceSourceViaImageDetails(
                    source_type='image',
                    image_id=image_id,
                    boot_volume_size_in_gbs=BOOT_GB
                ),
                create_vnic_details = oci.core.models.CreateVnicDetails(
                    subnet_id=subnet_id,
                    assign_public_ip=True,
                    hostname_label=DISPLAY_NAME
                ),
                metadata={ 'ssh_authorized_keys': SSH_KEY }
            )
        ).data
 
        log("")
        log("╔══════════════════════════════════════════╗")
        log("║   INSTANCE CREATED SUCCESSFULLY!        ║")
        log("╚══════════════════════════════════════════╝")
        log(f"Instance ID:   {result.id}")
        log(f"Display name:  {result.display_name}")
        log(f"State:         {result.lifecycle_state}")
        log("")
        log("Wait 2-3 minutes then check OCI console for public IP.")
        log("Then SCP your minecraft-server folder and run setup.sh!")
 
        sys.exit(1)  # exit 1 so GitHub emails you
 
    except oci.exceptions.ServiceError as e:
        code = e.code or ''
        msg  = e.message or str(e)
 
        if any(x in code + msg for x in ['InsufficientServiceCapacity', 'Out of host capacity', 'OutOfCapacity']):
            log(f"Out of capacity — will retry next run. ({code})")
            sys.exit(0)
 
        elif 'TooManyRequests' in code:
            log(f"Rate limited — will retry next run.")
            sys.exit(0)
 
        elif 'LimitExceeded' in code:
            if instance_exists():
                log(f"Resource limit exceeded, but an instance already exists. SUCCESS.")
                sys.exit(1)
            log(f"Resource limit exceeded, but no instance exists yet — will retry next run. ({code})")
            sys.exit(0)
 
        else:
            log(f"Unexpected error [{code}]: {msg}")
            sys.exit(1)
 
if __name__ == '__main__':
    main()
