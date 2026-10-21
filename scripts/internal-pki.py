#!/usr/bin/env python3
import pathlib,runpy
runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1]/"infra/buildkit/files/internal-pki.py"),run_name="__main__")
