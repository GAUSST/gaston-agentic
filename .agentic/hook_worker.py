#!/usr/bin/env python3
"""Private worker for project-enrolled native hooks."""
import eng

if __name__ == '__main__':
    eng.require_setup_complete()
    eng.hooks_command('process')
