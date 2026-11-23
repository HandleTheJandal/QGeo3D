# -*- coding: utf-8 -*-
"""Section definition (vertical slab) in map coordinates.

A section is the vertical plane through line A->B, with a slab of +/- width/2
either side. The viewing direction is n = z x u (u = unit A->B), so on screen
A is on the left and B on the right, and "forward" steps move the section
away from the viewer (+n), as in Micromine.
"""

import math


class Section:

    def __init__(self, a, b, width=10.0, name=""):
        self.a = (float(a[0]), float(a[1]))
        self.b = (float(b[0]), float(b[1]))
        self.width = float(width)
        self.name = name
        dx, dy = self.b[0] - self.a[0], self.b[1] - self.a[1]
        self.length = math.hypot(dx, dy)
        if self.length <= 0:
            raise ValueError("Section line has zero length")
        self.u = (dx / self.length, dy / self.length)          # along section
        self.n = (-self.u[1], self.u[0])                        # view direction (left normal)

    # ----------------------------------------------------------- properties
    @property
    def azimuth(self):
        """Bearing of A->B, degrees clockwise from grid north."""
        return math.degrees(math.atan2(self.u[0], self.u[1])) % 360.0

    @property
    def view_azimuth(self):
        """Bearing the viewer looks towards."""
        return math.degrees(math.atan2(self.n[0], self.n[1])) % 360.0

    @property
    def centre(self):
        return ((self.a[0] + self.b[0]) / 2.0, (self.a[1] + self.b[1]) / 2.0)

    # ------------------------------------------------------------- derived
    def offset(self, d):
        """Copy moved by d along the view direction (+ = away from viewer)."""
        ox, oy = self.n[0] * d, self.n[1] * d
        return Section((self.a[0] + ox, self.a[1] + oy), (self.b[0] + ox, self.b[1] + oy), self.width, self.name)

    def with_width(self, width):
        return Section(self.a, self.b, width, self.name)

    def slab_polygon(self):
        h = self.width / 2.0
        nx, ny = self.n[0] * h, self.n[1] * h
        a, b = self.a, self.b
        return [(a[0] - nx, a[1] - ny), (b[0] - nx, b[1] - ny), (b[0] + nx, b[1] + ny), (a[0] + nx, a[1] + ny)]

    def distance(self, x, y):
        """Signed perpendicular distance of a map point from the section plane (+ = away)."""
        return (x - self.a[0]) * self.n[0] + (y - self.a[1]) * self.n[1]

    def chainage(self, x, y):
        """Distance along A->B of the projection of a map point."""
        return (x - self.a[0]) * self.u[0] + (y - self.a[1]) * self.u[1]

    def to_spec(self, enabled=True, limit_ends=False, show_frame=True):
        return {"a": list(self.a), "b": list(self.b), "width": self.width,
                "enabled": bool(enabled), "limitEnds": bool(limit_ends), "showFrame": bool(show_frame)}

    def describe(self):
        cx, cy = self.centre
        return (f"Az {self.azimuth:05.1f}° (looking {self.view_azimuth:05.1f}°), "
                f"length {self.length:,.1f} m, width {self.width:g} m, centre {cx:,.1f} E {cy:,.1f} N")
