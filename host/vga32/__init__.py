"""Host side of the VGA32 thin client: the board is a screen + PS/2 input, apps run here."""
from .client import Device, Disconnected, Key, Mouse, discover
from .palette import C, PALETTE
from .screen import Screen

__all__ = ["Device", "Disconnected", "Key", "Mouse", "discover", "C", "PALETTE", "Screen"]
