"""The DIC camera: the camera process, its GUI-side client, clock sync and post-processing.

The camera scripts themselves live in <repo>/camera/ (swappable). Modules here
that run in the camera environment (service, simcam, jobs, scripts, sync)
import only the standard library and NumPy, so keep this file empty.
"""
