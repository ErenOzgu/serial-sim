"""A pseudo-terminal that stands in for a serial port, and the symbolic link that names it."""

from __future__ import annotations

import errno
import os
import tty
from pathlib import Path


class LinkError(Exception):
    """The device node or its link could not be set up."""


class PtyLink:
    """The master side belongs to the simulator, the slave side is what a program opens as its serial port.

    The simulator also keeps one slave descriptor open for its whole life. Without it the master would see
    "hang up" whenever the last client closes the port, and anything written while nobody has it open would
    be lost. With it, a greeting written at start waits in the buffer until a client reads it.
    """

    def __init__(self, link: Path) -> None:
        self.link = link
        self.master = self.slave = -1
        try:
            self.master, self.slave = os.openpty()
            tty.setraw(self.slave)
            os.set_blocking(self.master, False)
            self.node = os.ttyname(self.slave)
            self._point_link_at_node()
        except OSError as exc:
            self.close()
            raise LinkError(f"cannot create the device for {link}: {exc.strerror or exc}") from exc
        except LinkError:
            self.close()
            raise

    def _point_link_at_node(self) -> None:
        link = self.link
        if os.path.lexists(link) and not os.path.islink(link):
            raise LinkError(f"{link} exists and is not a symbolic link; it will not be replaced")
        temp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
        try:
            os.symlink(self.node, temp)
            os.replace(temp, link)
        except OSError as exc:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise LinkError(f"cannot create the link {link}: {exc.strerror or exc}") from exc

    def read(self) -> bytes:
        try:
            return os.read(self.master, 4096)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno == errno.EIO:  # nobody has the slave open; cannot happen while we hold one
                return b""
            raise

    def write(self, data: bytes) -> int:
        try:
            return os.write(self.master, data)
        except BlockingIOError:
            return 0

    def close(self) -> None:
        link = getattr(self, "node", None)
        if link and os.path.islink(self.link) and os.readlink(self.link) == self.node:
            try:
                os.unlink(self.link)
            except OSError:
                pass
        for fd in (self.master, self.slave):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.master = self.slave = -1
