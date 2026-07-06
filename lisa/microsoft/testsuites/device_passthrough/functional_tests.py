# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
import re
from typing import TYPE_CHECKING, Any, Dict, Tuple, Union, cast

from lisa import Environment, Node, TestCaseMetadata, TestSuite, TestSuiteMetadata
from lisa.base_tools import Cat
from lisa.operating_system import Windows
from lisa.platform_ import Platform
from lisa.sut_orchestrator import CLOUD_HYPERVISOR, HYPERV
from lisa.sut_orchestrator.util.schema import HostDevicePoolType
from lisa.testsuite import TestResult, simple_requirement
from lisa.tools import Lspci
from lisa.util import LisaException, SkippedException

if TYPE_CHECKING:
    from lisa.sut_orchestrator.hyperv.schema import (
        DeviceAddressSchema as HypervDeviceAddressSchema,
    )
    from lisa.sut_orchestrator.libvirt.ch_platform import CloudHypervisorPlatform
    from lisa.sut_orchestrator.libvirt.schema import (
        DeviceAddressSchema as LibvirtDeviceAddressSchema,
    )

    HostDeviceAddressSchema = Union[
        HypervDeviceAddressSchema,
        LibvirtDeviceAddressSchema,
    ]

SUPPORTED_PASSTHROUGH_PLATFORMS = [CLOUD_HYPERVISOR, HYPERV]


@TestSuiteMetadata(
    area="device_passthrough",
    category="functional",
    description="""
    This test suite is for testing device passthrough functional tests.
    """,
    requirement=simple_requirement(
        supported_platform_type=SUPPORTED_PASSTHROUGH_PLATFORMS,
        unsupported_os=[Windows],
    ),
)
class DevicePassthroughFunctionalTests(TestSuite):
    @TestCaseMetadata(
        description="""
            Check if passthrough device is visible to guest.
            This testcase supports the CLOUD_HYPERVISOR and HYPERV platforms
            of LISA. Please refer below runbook snippet.

            platform:
              - type: cloud-hypervisor
                cloud-hypervisor:
                  device_pools:
                    - type: "pci_net"
                      devices:
                        - vendor_id: xxx
                          device_id: xxx
                requirement:
                  cloud-hypervisor:
                    device_passthrough:
                      - pool_type: "pci_net"
                        managed: "yes"
                        count: 1

            We will check if sufficient devices are visible to guest or not.
            Platform will create device pool based on given device/vendor id.
            'device_passthrough' section will tell platform to create node
            with appropriate num of devices being passthrough. Based on pool_type
            value, platform will try to get devices from pool and assign it to node.

            Testcase will verify if needed devices are present on node by reading
            the runtime passthrough device context. It will resolve vendor/device
            ids for assigned host devices and check how many matching devices are
            present on the guest.
        """,
        priority=4,
        requirement=simple_requirement(
            supported_platform_type=SUPPORTED_PASSTHROUGH_PLATFORMS,
        ),
    )
    def verify_device_passthrough_on_guest(
        self,
        node: Node,
        environment: Environment,
        result: TestResult,
    ) -> None:
        lspci = node.tools[Lspci]
        node_context = self._get_node_context(environment, node)

        expected_devices = self._get_expected_devices(environment, node_context)
        self._verify_devices_in_guest(lspci, expected_devices)

    @TestCaseMetadata(
        description="""
            Check if two NIC passthrough devices are visible to one guest.

            This testcase validates the generic device passthrough path for a
            runbook that assigns two pci_net devices to a single guest. It reads
            the runtime passthrough context, confirms at least two pci_net
            devices were assigned, then verifies the assigned vendor/device IDs
            are visible inside the guest with lspci.
        """,
        priority=4,
        requirement=simple_requirement(
            supported_platform_type=SUPPORTED_PASSTHROUGH_PLATFORMS,
        ),
    )
    def verify_dual_device_passthrough_on_guest(
        self,
        node: Node,
        environment: Environment,
        result: TestResult,
    ) -> None:
        lspci = node.tools[Lspci]
        node_context = self._get_node_context(environment, node)

        expected_devices = self._get_expected_devices(
            environment,
            node_context,
            pool_type_filter=HostDevicePoolType.PCI_NIC.value,
            min_count=2,
        )
        self._verify_devices_in_guest(lspci, expected_devices)

    def _get_node_context(self, environment: Environment, node: Node) -> Any:
        platform = environment.platform
        if platform is None:
            raise SkippedException(
                "Device passthrough validation requires a LISA platform context. "
                "Verify the runbook uses cloud-hypervisor or hyperv."
            )
        platform_name = platform.type_name()
        node_context: Any

        if platform_name == CLOUD_HYPERVISOR:
            # Import at runtime to avoid libvirt dependency on other platforms.
            from lisa.sut_orchestrator.libvirt.context import (
                get_node_context as get_libvirt_node_context,
            )

            node_context = get_libvirt_node_context(node)
        elif platform_name == HYPERV:
            from lisa.sut_orchestrator.hyperv.context import (
                get_node_context as get_hyperv_node_context,
            )

            node_context = get_hyperv_node_context(node)
        else:
            raise SkippedException(
                f"Device passthrough validation is not supported on '{platform_name}'"
            )

        if not node_context.passthrough_devices:
            raise SkippedException("No passthrough devices are assigned to node")

        return node_context

    def _get_expected_devices(
        self,
        environment: Environment,
        node_context: Any,
        pool_type_filter: str = "",
        min_count: int = 1,
    ) -> Dict[Tuple[str, str, str], int]:
        platform = environment.platform
        assert platform is not None
        expected_devices: Dict[Tuple[str, str, str], int] = {}
        matching_device_count = 0
        for passthrough_context in node_context.passthrough_devices:
            pool_type = str(passthrough_context.pool_type.value)
            if pool_type_filter and pool_type != pool_type_filter:
                continue

            if not passthrough_context.device_list:
                raise LisaException(
                    f"No devices assigned to node for pool type: {pool_type}"
                )
            for host_device in passthrough_context.device_list:
                vendor_device_id = self._vendor_device_from_host_device(
                    platform, host_device
                )
                key = (
                    pool_type,
                    vendor_device_id["vendor_id"],
                    vendor_device_id["device_id"],
                )
                expected_devices[key] = expected_devices.get(key, 0) + 1
                matching_device_count += 1

        if matching_device_count < min_count:
            pool_type_message = (
                f" for pool type '{pool_type_filter}'" if pool_type_filter else ""
            )
            raise SkippedException(
                f"Device passthrough validation requires at least {min_count} "
                f"assigned device(s){pool_type_message}, found "
                f"{matching_device_count}."
            )

        return expected_devices

    @staticmethod
    def _verify_devices_in_guest(
        lspci: Lspci,
        expected_devices: Dict[Tuple[str, str, str], int],
    ) -> None:
        for (pool_type, ven_id, dev_id), expected_count in expected_devices.items():
            devices = lspci.get_devices_by_vendor_device_id(
                vendor_id=ven_id,
                device_id=dev_id,
                force_run=True,
            )
            if len(devices) < expected_count:
                raise LisaException(
                    f"Passthrough device validation failed for "
                    f"pool_type '{pool_type}': Found {len(devices)} "
                    f"device(s) but expected {expected_count}. "
                    f"Vendor/Device ID: {ven_id}:{dev_id}"
                )

    @staticmethod
    def _vendor_device_from_host_device(
        platform: Platform,
        device: "HostDeviceAddressSchema",
    ) -> Dict[str, str]:
        platform_name = platform.type_name()
        if platform_name == HYPERV:
            hyperv_device = cast("HypervDeviceAddressSchema", device)
            instance_id = hyperv_device.instance_id
            match = re.search(
                r"VEN_(?P<vendor_id>[0-9A-Fa-f]{4})&"
                r"DEV_(?P<device_id>[0-9A-Fa-f]{4})",
                instance_id,
            )
            if not match:
                raise LisaException(
                    f"Cannot resolve vendor/device id from Hyper-V host device "
                    f"instance id: {instance_id}"
                )
            return {
                "vendor_id": match.group("vendor_id").lower(),
                "device_id": match.group("device_id").lower(),
            }

        if platform_name != CLOUD_HYPERVISOR:
            raise LisaException(
                f"Device passthrough host device lookup is not supported on "
                f"'{platform_name}'. Use a cloud-hypervisor or hyperv platform."
            )

        cloud_hypervisor = cast("CloudHypervisorPlatform", platform)
        libvirt_device = cast("LibvirtDeviceAddressSchema", device)
        bdf = (
            f"{libvirt_device.domain}:{libvirt_device.bus}:"
            f"{libvirt_device.slot}.{libvirt_device.function}"
        ).lower()
        cat = cloud_hypervisor.host_node.tools[Cat]
        vendor_raw = cat.read(f"/sys/bus/pci/devices/{bdf}/vendor", sudo=True).strip()
        device_raw = cat.read(f"/sys/bus/pci/devices/{bdf}/device", sudo=True).strip()
        # Normalize to 4-digit lowercase hex used by lspci identifiers.
        vendor_id = vendor_raw.lower().replace("0x", "").zfill(4)
        device_id = device_raw.lower().replace("0x", "").zfill(4)
        return {"vendor_id": vendor_id, "device_id": device_id}
