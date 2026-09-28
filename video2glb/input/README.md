# Input videos

This folder includes 17 source MP4 videos, including `Train.mp4`, so a clone
contains the inputs needed for the documented examples. You can also place
additional MP4 videos here. MP4 files directly in this folder are tracked by
Git; generated videos elsewhere remain ignored.

Complete the [setup instructions](../README.md#setup-windows) before running.

From PowerShell in the project root (`D:\video2glb` on the original computer),
run the following using the project's existing Python environment:

```powershell
.\.venv\Scripts\python.exe convert.py --video ".\input\Train.mp4"
```

Replace `Train.mp4` with another video's filename as needed.

To process all supported videos in this folder:

```powershell
.\.venv\Scripts\python.exe convert.py --input-dir ".\input" --batch
```

Successful results are written under `output/`; inspect the reported status
for videos that fail validation. See the [project README](../README.md) for
output details and [settings](../config/settings.yaml) for avatar, model,
and Blender paths. Dependencies and Blender must be installed locally.

GitHub displays these instructions; this repository currently has no web
upload form or GitHub workflow for running the converter.
