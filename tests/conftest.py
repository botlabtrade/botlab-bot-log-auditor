import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SAMPLES = os.path.join(ROOT, "samples")


def read_sample(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as f:
        return f.read()
