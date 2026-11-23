#!/usr/bin/env python3
"""Internal pipeline entry; no public ./dev subcommand."""
import sys
import eng

if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[2] not in ('codex','claude') or sys.argv[3] not in ('codex','claude'):
        raise SystemExit('Invalid internal task parameters')
    try:
        eng.run_automated_task(sys.argv[1],builder=sys.argv[2],reviewer=sys.argv[3])
    except (RuntimeError,OSError) as exc:
        if eng.ACTIVE_RUN is not None:
            eng.save_state(eng.ACTIVE_RUN,'blocked',reason=str(exc))
        print('ERROR:',exc,file=sys.stderr)
        raise SystemExit(2)
    finally:
        if eng.ACTIVE_RUN is not None and (eng.ACTIVE_RUN/'task.json').exists():
            try:
                rd=eng.ACTIVE_RUN
                eng.write_summary(rd)
                state_file=rd/'state.json'
                if state_file.exists() and eng.load_json(state_file).get('stage') in (
                    'ready_for_human_release_review','changes_requested','checks_failed',
                    'checks_passed_review_missing'):
                    eng.remember_verified_tree(rd.name)
                eng.ensure_local_context()
                print('\nFINAL DEVELOPMENT SUMMARY\n'+eng.make_summary(rd),flush=True)
                print('Detailed evidence:',(rd/'SUMMARY.md').relative_to(eng.ROOT),flush=True)
            except Exception as exc:
                print('Could not produce final summary:',exc,file=sys.stderr)
