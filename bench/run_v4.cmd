@echo off
rem Runs the three pre-registered v4 batches sequentially (each is resumable: re-run this file to resume).
cd /d "%~dp0.."
set PYTHONPATH=src
set PYTHONIOENCODING=utf-8
python -u -m bench.run --model claude-haiku-4-5-20251001 --out bench/results/e1-haiku.jsonl --max-total-usd 14 > bench\results\e1-haiku.log 2> bench\results\e1-haiku.err
python -u -m bench.run --scenarios twins-fresh,twins-drift --arms A,B,C,C2 --tasks T1,T5 --out bench/results/e2-sonnet.jsonl --max-total-usd 14 > bench\results\e2-sonnet.log 2> bench\results\e2-sonnet.err
python -u -m bench.run --model claude-haiku-4-5-20251001 --scenarios twins-fresh,twins-drift --arms A,B,C,C2 --tasks T1,T5 --out bench/results/e2-haiku.jsonl --max-total-usd 14 > bench\results\e2-haiku.log 2> bench\results\e2-haiku.err
echo done > bench\results\v4-done.flag
