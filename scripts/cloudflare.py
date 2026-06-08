import argparse
import ipaddress
import subprocess
import sys
from urllib.error import URLError
from urllib.request import Request, urlopen


CLOUDFLARE_IPS_URLS = (
    "https://www.cloudflare.com/ips-v4/",
    "https://www.cloudflare.com/ips-v6/",
)


def fetch_networks():
    networks = []

    for url in CLOUDFLARE_IPS_URLS:
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 cloudflare-ufw-sync",
                },
            )
            with urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
        except URLError as exc:
            raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc

        for line in body.splitlines():
            value = line.strip()
            if not value:
                continue

            try:
                networks.append(ipaddress.ip_network(value, strict=True))
            except ValueError as exc:
                raise RuntimeError(f"Invalid network from {url}: {value}") from exc

    return networks


def allow_network(network, dry_run):
    command = [
        "ufw",
        "allow",
        "from",
        str(network),
        "comment",
        "cloudflare",
    ]

    if dry_run:
        print(" ".join(command))
        return

    subprocess.run(command, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Allow Cloudflare IPv4 and IPv6 ranges in UFW."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print ufw commands without applying them.",
    )
    args = parser.parse_args()

    try:
        networks = fetch_networks()
        for network in networks:
            allow_network(network, args.dry_run)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"Processed {len(networks)} Cloudflare networks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
