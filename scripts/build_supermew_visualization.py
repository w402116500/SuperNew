# -*- coding: utf-8 -*-
"""Generate supermew_visualization.html for SuperMew Enterprise RAG System."""
import os
import sys

cur_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(cur_dir, "viz_builder"))
import build

if __name__ == "__main__":
    build.build()
