"""Unit tests for the MAPDL .cdb reader (no model files needed)."""

import tempfile
import unittest
from pathlib import Path

from twin_worker import cdb

# Two bodies in separate EBLOCKs (as Workbench writes them): a hex and a
# degenerate 20-node hex that is really a tet; plus a contact element block to skip.
CDB = """et,1,185
et,2,186
et,3,174
nblock,3,,9
(1i9,3e20.9e3)
        1     0.000000000E+00     0.000000000E+00     0.000000000E+00
        2     1.000000000E+00     0.000000000E+00     0.000000000E+00
        3     1.000000000E+00     1.000000000E+00     0.000000000E+00
        4     0.000000000E+00     1.000000000E+00     0.000000000E+00
        5     0.000000000E+00     0.000000000E+00     1.000000000E+00
        6     1.000000000E+00     0.000000000E+00     1.000000000E+00
        7     1.000000000E+00     1.000000000E+00     1.000000000E+00
        8     0.000000000E+00     1.000000000E+00     1.000000000E+00
        9     2.000000000E+00     0.000000000E+00     0.000000000E+00
N,R5.3,LOC,       -1,
eblock,19,solid,,1
(19i9)
        1        1        1        1        0        0        0        0        8        0        1        1        2        3        4        5        6        7        8
       -1
eblock,19,solid,,1
(19i9)
        1        2        1        1        0        0        0        0       20        0        2        2        9        3        3        6        6        6        6
        0        0        0        0        0        0        0        0        0        0        0        0
       -1
eblock,19,solid,,1
(19i9)
        1        3        1        1        0        0        0        0        4        0        3        1        2        3        4
       -1
"""


class CdbTest(unittest.TestCase):
    def test_all_blocks_and_degenerate_cells(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "m.cdb"
            path.write_text(CDB)
            points, cells = cdb.read_cdb(path)
        self.assertEqual(points.shape, (9, 3))
        self.assertEqual([c[0] for c in cells], [cdb.VTK_HEXAHEDRON, cdb.VTK_TETRA])
        self.assertEqual(cells[1][1], [1, 8, 2, 5])  # node ids 2, 9, 3, 6 -> indices

    def test_degenerate_shapes(self):
        self.assertEqual(cdb._linear_cell([1, 2, 3, 4, 5, 5, 5, 5])[0], cdb.VTK_PYRAMID)
        self.assertEqual(cdb._linear_cell([1, 2, 3, 3, 4, 5, 6, 6])[0], cdb.VTK_WEDGE)
        self.assertEqual(cdb._linear_cell(list(range(1, 11)))[0], cdb.VTK_TETRA)

    def test_format_widths(self):
        self.assertEqual(cdb._field_widths("(1i9,3e20.9e3)"), [9, 20, 20, 20])
        self.assertEqual(cdb._field_widths("(19i9)"), [9] * 19)


if __name__ == "__main__":
    unittest.main()
