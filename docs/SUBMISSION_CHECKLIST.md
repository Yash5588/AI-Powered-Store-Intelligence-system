# Submission Checklist

## HackerEarth Fields

- Title: use `AI-Powered Store Intelligence System from CCTV`.
- Description: copy the description from `SUBMISSION.md`.
- Theme: select the Purplle Round 2 problem statement theme.
- Snapshots: upload 2-4 screenshots from the React dashboard after seeding demo data.
- Video URL: upload a short demo video to Drive/YouTube/Loom and paste the public link.
- Presentation: export `docs/PITCH_DECK.md` into slides or PDF.
- Demo Link: paste deployed demo URL if hosted; otherwise use the repository URL or demo video URL if the field requires a URL.
- Repository URL: paste the GitHub repository URL.
- Source Code: upload a clean ZIP excluding private/heavy artifacts.
- Instructions to Run: copy the run instructions from `SUBMISSION.md`.

## Screenshot Suggestions

- Overview page after demo data is seeded.
- Video Processing page with upload/process controls.
- Heatmap page.
- Anomalies or Funnel page.

## Final Local Checks

```powershell
..\.venv\Scripts\python.exe -m pytest
cd frontend
npm run build
```

```powershell
docker compose up --build
```

## Privacy Checks

Do not upload or commit:

- Real CCTV files.
- Raw private datasets.
- Model weights such as `*.pt`.
- SQLite database files.
- Generated annotated videos.
- Virtual environment or `node_modules`.

