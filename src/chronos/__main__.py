"""Entry point: chronos CLI."""
import sys


def main():
    """Main entry point for the chronos CLI."""
    from chronos.cli.main import cli
    cli()


if __name__ == "__main__":
    main()
