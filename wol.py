"""Sends a Wake-on-LAN magic packet."""
import socket


def send_magic_packet(mac: str, broadcast_ip: str, port: int = 9):
    """A magic packet is 6 bytes of 0xFF followed by the target MAC
    repeated 16 times, sent as a single UDP datagram to the LAN broadcast
    address. Any device on the segment with WoL enabled wakes on seeing it."""
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(mac_bytes) != 6:
        raise ValueError(f"'{mac}' is not a valid MAC address")

    packet = b"\xff" * 6 + mac_bytes * 16

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.sendto(packet, (broadcast_ip, port))
    finally:
        sock.close()


if __name__ == "__main__":
    # Quick manual test: python wol.py <mac> <broadcast_ip>
    import sys
    send_magic_packet(sys.argv[1], sys.argv[2])
    print("sent")
