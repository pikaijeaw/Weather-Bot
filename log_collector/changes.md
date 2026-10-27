# Auto-tune / auto-fix history

| date | change | evidence |
|---|---|---|
| 2026-10-24 | max_edge 0.25 -> 0.30 | 0.18+ bucket n=19, win 68%, ROI +30% (20 settled overall, ROI +23.9%) |
| 2026-10-27 | fix: parse_station() for new METAR rules format; ignore SPECI; collect.py BLIND flag | 0 candidates in every scan 10-23..10-27 (215/221 events had no station) |
