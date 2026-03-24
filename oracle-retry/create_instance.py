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
from datetime import datetime

def log(msg):
    print(f"[{datetime.utcnow().strftime('%H:%M:%S UTC')}] {msg}", flush=True)

COMPARTMENT_ID = os.environ['OCI_COMPARTMENT_ID']
SSH_KEY        = os.environ['SSH_PUBLIC_KEY']
DISPLAY_NAME   = 'minecraft-server'
SHAPE          = 'VM.Standard.A1.Flex'
OCPUS          = 4
MEMORY_GB      = 24
BOOT_GB        = 50

config  = oci.config.from_file()
compute = oci.core.ComputeClient(config)
network = oci.core.VirtualNetworkClient(config)
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

def get_subnet():
    vcns = network.list_vcns(compartment_id=COMPARTMENT_ID).data
    if not vcns:
        raise Exception("No VCN found. Create one in OCI console first.")
    vcn = vcns[0]
    subnets = network.list_subnets(
        compartment_id=COMPARTMENT_ID,
        vcn_id=vcn.id
    ).data
    if not subnets:
        raise Exception("No subnet found in VCN.")
    return subnets[0].id

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
        sys.exit(1)  # exit 1 = triggers GitHub email notification

    log("No instance found. Discovering resources...")
    ad        = get_availability_domain()
    image_id  = get_ubuntu_arm_image()
    subnet_id = get_subnet()
    log(f"AD: {ad}")
    log(f"Image: {image_id}")
    log(f"Subnet: {subnet_id}")

    log(f"Attempting to create {SHAPE} ({OCPUS} OCPU / {MEMORY_GB}GB RAM)...")
    try:
        result = compute.launch_instance(
            oci.core.models.LaunchInstanceDetails(
                compartment_id    = COMPARTMENT_ID,
                availability_domain = ad,
                display_name      = DISPLAY_NAME,
                shape             = SHAPE,
                shape_config      = oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=OCPUS, memory_in_gbs=MEMORY_GB
                ),
                source_details    = oci.core.models.InstanceSourceViaImageDetails(
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

        log(f"")
        log(f"╔══════════════════════════════════════════╗")
        log(f"║   INSTANCE CREATED SUCCESSFULLY!        ║")
        log(f"╚══════════════════════════════════════════╝")
        log(f"Instance ID:   {result.id}")
        log(f"Display name:  {result.display_name}")
        log(f"State:         {result.lifecycle_state}")
        log(f"")
        log(f"Wait 2-3 minutes then check OCI console for public IP.")
        log(f"Then SCP your minecraft-server folder and run setup.sh!")

        # Exit 1 so GitHub marks this run as "failed" and emails you
        sys.exit(1)

    except oci.exceptions.ServiceError as e:
        code = e.code or ''
        msg  = e.message or str(e)

        if any(x in code + msg for x in ['InsufficientServiceCapacity', 'Out of host capacity', 'OutOfCapacity']):
            log(f"Out of capacity — will retry next run. ({code})")
            sys.exit(0)  # expected, don't spam GitHub notifications

        elif 'TooManyRequests' in code:
            log(f"Rate limited — will retry next run.")
            sys.exit(0)

        elif 'LimitExceeded' in code:
            log(f"Resource limit exceeded. You may already have a free-tier instance.")
            log(f"Check OCI Console → Compute → Instances.")
            sys.exit(1)  # notify, something to investigate

        else:
            log(f"Unexpected error [{code}]: {msg}")
            sys.exit(1)  # notify so you can investigate

if __name__ == '__main__':
    main()
