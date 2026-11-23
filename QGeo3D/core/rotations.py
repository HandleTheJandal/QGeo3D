# -*- coding: utf-8 -*-
"""Block model rotations as model -> world matrices (world = origin + R @ model).

Bearing / plunge / dip (Vulcan and Surpac): here the bearing is the azimuth of the model X axis (90 =
east = unrotated; Vulcan's own bearing; Surpac quotes the Y axis, so it is passed as bearing + 90); plunge tilts the X axis down (positive = down), about the model Y axis; dip tilts
the Y axis down (positive = down), about the model X axis. Vulcan applies bearing, plunge, dip;
Surpac bearing, dip, plunge. Datamine angles: core/dmfile.py (_axis_rot, datamine_angles)."""

import numpy as np


def _tilt(a, b, deg):
    """Turn axis a towards -b by deg (b turns towards a) - both unit vectors."""
    t = np.radians(deg)
    return a * np.cos(t) - b * np.sin(t), b * np.cos(t) + a * np.sin(t)


def bearing_plunge_dip(bearing=90.0, plunge=0.0, dip=0.0, order="vulcan"):
    b = np.radians(bearing)
    X = np.array([np.sin(b), np.cos(b), 0.0])
    Y = np.array([-np.cos(b), np.sin(b), 0.0])
    Z = np.array([0.0, 0.0, 1.0])
    steps = (("plunge", plunge), ("dip", dip)) if order == "vulcan" else (("dip", dip), ("plunge", plunge))
    for what, deg in steps:
        if abs(deg) < 1e-12:
            continue
        if what == "plunge":
            X, Z = _tilt(X, Z, deg)               # X goes down for positive plunge
        else:
            Y, Z = _tilt(Y, Z, deg)               # Y goes down for positive dip
    return np.column_stack([X, Y, Z])
