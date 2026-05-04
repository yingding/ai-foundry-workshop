# Notebook Editing Rules for This Workspace

## Critical: Verify cell edits don't scramble code

When using `edit_notebook_file` to replace cell content, the tool occasionally
scrambles lines (interleaving statements, moving print() into function args, etc.).

### Before submitting any cell edit:
1. **Read back the cell** after editing to verify correctness
2. **Check these common scramble patterns**:
   - `function_call(\n    arg1,print(...)` — print merged into function args
   - `create_version(\n\n    agent_name=...,print(...)` — blank lines + merged statements
   - `.then(\nif __name__` — Gradio `.then()` merged with launch block
   - Lines from the bottom of the cell appearing in the middle

### Prevention:
- For cells with `create_version()` + `print()`, always write them as one clean block
- For Gradio cells, keep `.submit().then()`, `chatbot.clear()`, and `demo.launch()` as distinct blocks
- After editing a cell, do a quick `read_file` on the edited line range to confirm

### Known fragile cells in 02-medical-diagnostic-agent.ipynb:
- **Cell 17** (agent create_version): The `instructions` string + `create_version()` + `print()` 
  gets scrambled frequently. Always use `edit_notebook_file` for the full cell, not `replace_string_in_file`.
- **Cell 25** (Gradio UI): The `.submit().then()` chain, `demo.close()`, and `demo.launch()` 
  blocks get interleaved. Same — always replace the full cell.

### Gradio port management pattern (both notebooks):
```python
# At top of Gradio cell, before gr.Blocks():
GRADIO_PORT = 7861  # or 7860 for notebook 01
if 'demo' in globals():
    try:
        demo.close()
        print(f"closed previous Gradio server on port {GRADIO_PORT}")
    except Exception:
        pass
```
This closes the OLD demo instance before `with gr.Blocks() as demo:` creates the new one.

### Sandbox image stripping:
The regex `_SANDBOX_IMG_RE = re.compile(r'!?\[[^\]]*\]\(sandbox:/[^)]*\)')` 
strips both `![img](sandbox:/...)` and `[link](sandbox:/...)` from agent text.

### Code interpreter file upload:
- `FORCE_REUPLOAD = True` deletes old files by name and re-uploads fresh CSVs
- Only `.csv` files are uploaded (skip metadata.md)
- Set to `False` after data is stable

## Image handling strategy (whitelist, not blacklist)

The code interpreter agent embeds image references in its streamed text using
various formats: `sandbox:/mnt/data/...`, bare filenames like `plot.png`,
`/mnt/data/output.png`, or even `<img>` tags. **None of these work in Gradio.**

The ONLY valid images come from `container_file_citation` annotations, which we
download via the containers API and inject as `data:image/png;base64,...` URIs.

### Decision: whitelist `data:` URIs, strip everything else
Instead of blacklisting each broken pattern one at a time (which failed repeatedly),
we use a **whitelist** approach:

```python
_BROKEN_IMG_RE = re.compile(r'!?\[[^\]]*\]\((?!data:)[^)]*\)')
_BROKEN_HTML_RE = re.compile(r'<img[^>]*(?!data:)[^>]*>', re.IGNORECASE)
```

This strips ALL `![...](URL)` and `<img>` tags where the URL does NOT start with
`data:`. Since the only valid images are our base64 data URIs, everything else is
guaranteed broken. Applied in both:
- `message done` handler (final cleanup)
- `response.output_text.delta` handler (live stripping during streaming)

### Why not temp files?
We tried saving images to temp files and using local paths — Gradio couldn't serve
them without `allowed_paths` config, and base64 inline worked more reliably. The
trade-off is larger message payloads but no file-serving issues.

## Agent instruction design principles

### Principle-oriented, not prescriptive
The agent instructions in cell 17 follow a principle-oriented style:
- Tell the agent WHAT to do (LOF, scatter, feature selection) not HOW exactly
- Let the code interpreter choose implementation details (marker colors, PCA vs variance)
- Only constrain what causes failures (e.g., "exclude binary columns from axes")

### Multi-tool workflow
The instructions enforce a strict order: code_interpreter → file_search → synthesize.
This ensures the file_search query is informed by actual patient data from Step 1.

### Data handling
Instructions tell the agent to auto-impute missing data (median for numeric, mode for
categorical) and never ask the user for confirmation. This prevents the agent from
stalling mid-analysis.

### Chart generation
The `matplotlib.use('Agg')` + `plt.savefig()` pattern is mandatory because the code
interpreter sandbox has no display. `plt.show()` causes silent failures.

## Mock data schema (medicare-data/)
- `lung_cancer_patient_clinical_features.csv`: 3000 patients, PATIENT_ID 0-2999, 
  risk factors as Yes/No strings, AGE as int, LUNG_CANCER as YES/NO
- `lung_cancer_patients_symptoms_2025.csv`: same 3000 patients, CANCER_STAGE, 
  TUMOR_SIZE_CM (float), DIAGNOSIS_DATE, TREATMENT_RECEIVED
- Join on PATIENT_ID (inner join). See `medicare-data/metadata.md` for full schema.
