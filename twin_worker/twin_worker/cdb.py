"""Minimal MAPDL input/archive (.cdb/.dat) mesh reader.

``ansys-mapdl-reader``'s Archive only reads the first EBLOCK, while Workbench
exports one block per body. This reads every NBLOCK/EBLOCK and keeps solid
elements as linear cells (corner nodes only), which is all surface extraction needs.
"""

import re

import numpy as np

# Linear VTK cell types.
VTK_TETRA = 10
VTK_HEXAHEDRON = 12
VTK_WEDGE = 13
VTK_PYRAMID = 14

# 3D solid element types (structural and thermal), by MAPDL type number.
SOLID_TYPES = {45, 70, 87, 90, 185, 186, 187, 285, 278, 279}

_FORMAT_FIELD = re.compile(r"(\d*)([ie])(\d+)", re.IGNORECASE)


def _field_widths(fmt_line):
    """``(1i9,3e20.9e3)`` -> [9, 20, 20, 20]."""
    widths = []
    for token in fmt_line.strip().strip("()").split(","):
        m = _FORMAT_FIELD.match(token.strip())
        if m:
            widths += [int(m.group(3))] * int(m.group(1) or 1)
    return widths


def _split_fixed(line, width):
    line = line.rstrip("\r\n")
    return [line[i:i + width] for i in range(0, len(line), width) if line[i:i + width].strip()]


def _linear_cell(nodes):
    """Corner nodes of a (possibly degenerate) MAPDL solid -> (vtk type, node list)."""
    n = len(nodes)
    if n in (4, 10):
        return VTK_TETRA, nodes[:4]
    if n not in (8, 20):
        return None
    c = nodes[:8]
    # MAPDL degenerate hexes: K=L for prism/tet, M=N=O=P for pyramid/tet.
    if c[2] == c[3] and c[4] == c[5] == c[6] == c[7]:
        return VTK_TETRA, [c[0], c[1], c[2], c[4]]
    if c[4] == c[5] == c[6] == c[7]:
        return VTK_PYRAMID, c[:5]
    if c[2] == c[3] and c[6] == c[7]:
        return VTK_WEDGE, [c[0], c[1], c[2], c[4], c[5], c[6]]
    return VTK_HEXAHEDRON, c


def read_cdb(path):
    """Return ``(points (N,3) float64, cells list[(vtk_type, [point indices])])``."""
    with open(path, "r", errors="replace") as f:
        lines = f.readlines()

    et_types = {}
    node_ids, coords = [], []
    raw_cells = []  # (vtk type, [node ids])
    i, n_lines = 0, len(lines)
    while i < n_lines:
        line = lines[i]
        head = line.split(",")[0].strip().lower()
        if head == "et":
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3 and parts[1].isdigit():
                et_types[int(parts[1])] = int(re.match(r"\d+", parts[2]).group())
            i += 1
        elif head == "nblock":
            widths = _field_widths(lines[i + 1])
            i += 2
            while i < n_lines and not lines[i].lstrip().upper().startswith(("N,", "-1")):
                text = lines[i].rstrip("\r\n")
                fields, pos = [], 0
                for w in widths:
                    fields.append(text[pos:pos + w])
                    pos += w
                node_ids.append(int(fields[0]))
                xyz = [float(v) if v.strip() else 0.0 for v in fields[1:4]]
                coords.append(xyz + [0.0] * (3 - len(xyz)))
                i += 1
        elif head == "eblock" and "solid" in line.lower():
            width = _field_widths(lines[i + 1])[0]
            i += 2
            while i < n_lines and not lines[i].strip().startswith("-1"):
                fields = [int(v) for v in _split_fixed(lines[i], width)]
                i += 1
                nnodes = fields[8]
                nodes = fields[11:]
                while len(nodes) < nnodes and i < n_lines:
                    nodes += [int(v) for v in _split_fixed(lines[i], width)]
                    i += 1
                if et_types.get(fields[1]) in SOLID_TYPES:
                    cell = _linear_cell(nodes[:nnodes])
                    if cell is not None:
                        raw_cells.append(cell)
        else:
            i += 1

    index = {nid: k for k, nid in enumerate(node_ids)}
    cells = [(vtype, [index[n] for n in nodes]) for vtype, nodes in raw_cells]
    return np.asarray(coords, dtype=np.float64), cells


def to_pyvista(points, cells):
    import pyvista as pv

    conn = []
    types = []
    for vtype, nodes in cells:
        conn += [len(nodes), *nodes]
        types.append(vtype)
    return pv.UnstructuredGrid(np.asarray(conn, dtype=np.int64), np.asarray(types, dtype=np.uint8), points)
