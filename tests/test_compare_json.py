"""Guard the numerical validation helper against false equivalence."""
import argparse
import unittest
from tests.compare_json import walk


class Comparison(unittest.TestCase):
    def differences(self,a,b):
        out={'diffs':[],'floats':0,'max_abs':0.0}
        walk(a,b,'',argparse.Namespace(ignore=[],atol=1e-12,rtol=1e-9),out)
        return out['diffs']

    def test_nonfinite_and_discrete_types(self):
        for a,b in [(float('nan'),7.0),(float('inf'),7.0),(float('inf'),float('-inf')),
                    (True,1),(False,0),(True,1.0),(1.0,'1')]:
            with self.subTest(a=a,b=b):
                self.assertTrue(self.differences(a,b))
        for a,b in [(float('nan'),float('nan')),(float('inf'),float('inf')),(1.0,1.0+1e-12),(True,True)]:
            self.assertFalse(self.differences(a,b))


if __name__=='__main__':
    unittest.main()
