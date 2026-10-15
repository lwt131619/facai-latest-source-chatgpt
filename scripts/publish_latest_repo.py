"""Publish to the existing user-selected repo; never create or change repositories."""
from checkpoint import checkpoint


if __name__ == '__main__':
    raise SystemExit(checkpoint(validation='partial'))
