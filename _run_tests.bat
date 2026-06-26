@echo off
cd /d d:\gc\forge-agent
python -m pytest test_plan_mode.py -x -k "analysis" --tb=short -q