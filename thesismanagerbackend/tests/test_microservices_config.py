"""Regression guards for the optional microservices Compose profile."""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class MicroservicesGatewayTests(SimpleTestCase):
    """Keep the gateway configuration aligned with the service processes."""

    def setUp(self) -> None:
        root = Path(settings.BASE_DIR)
        self.compose = (root / "docker-compose.yml").read_text()
        self.nginx = (root / "docker" / "nginx.conf").read_text()

    def test_nginx_upstreams_point_to_service_ports(self) -> None:
        upstreams = {
            name: (host, int(port))
            for name, host, port in re.findall(
                r"upstream\s+([\w]+)\s*\{\s*server\s+([\w-]+):(\d+);\s*\}", self.nginx
            )
        }
        self.assertEqual(len(upstreams), 6)

        service_ports = {
            name: int(port)
            for name, port in re.findall(
                r"^  ([\w-]+-service):\n.*?runserver\", \"0\.0\.0\.0:(\d+)",
                self.compose,
                re.MULTILINE | re.DOTALL,
            )
        }
        self.assertEqual(len(service_ports), 6)
        for upstream, (host, port) in upstreams.items():
            service = upstream.replace("_", "-")
            self.assertEqual(host, service)
            self.assertEqual(port, service_ports[service], f"upstream {upstream}")

    def test_gateway_and_backend_publish_different_host_ports(self) -> None:
        gateway = self._service_block("gateway")
        backend = self._service_block("backend")
        gateway_port = re.search(r"127\.0\.0\.1:\$\{GATEWAY_PORT:-(\d+)\}:8000", gateway)
        backend_port = re.search(r"127\.0\.0\.1:\$\{BACKEND_PORT:-(\d+)\}:8000", backend)
        self.assertIsNotNone(gateway_port)
        self.assertIsNotNone(backend_port)
        assert gateway_port is not None and backend_port is not None
        self.assertNotEqual(gateway_port.group(1), backend_port.group(1))

    def _service_block(self, name: str) -> str:
        match = re.search(rf"^  {name}:\n((?:[ \t]+.*\n|\n)*)", self.compose, re.MULTILINE)
        self.assertIsNotNone(match)
        assert match is not None
        return match.group(1)
