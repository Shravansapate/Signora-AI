# Pipeline output

The converter creates result folders here automatically. Existing output
files are not inputs to a new conversion and are not needed to run the
pipeline. This README keeps the output folder visible on GitHub; generated
results stay on the computer running the converter.

After completing the [setup instructions](../README.md#setup-windows), run
this from the project root:

```powershell
.\.venv\Scripts\python.exe convert.py --video ".\input\Train.mp4"
```

Inspect `output/TRAIN/` for the generated motion data, QC reports, and, when
the relevant stages complete successfully, `Train.glb` and debug previews.
Per-run evidence is stored under the corresponding `runs/` directory.
Failed GLBs may be preserved under `failed/` instead of the final output path.

For a batch run, inspect `output/batch_summary.json` for each video's status.
An output file alone does not mean that validation passed: check its QC and
validation reports. `REVIEW` or `FAIL` results require further investigation;
ISL production approval also requires qualified signer review.

The pipeline creates `temp/`, `logs/`, and `failed/` as needed. These runtime
directories and previous generated output files are excluded from Git.
