# -*- coding: utf-8 -*-
"""Generate ragtrail_visualization.html for RagTrail Enterprise RAG System."""
import os
import sys

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(cur_dir, "viz_builder"))
import build

if __name__ == "__main__":
    build.build()
