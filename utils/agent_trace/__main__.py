import sys


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        from utils.agent_trace.server import cmd_serve
        import argparse
        p = argparse.ArgumentParser(prog="agent_trace serve")
        p.add_argument("command")
        p.add_argument("-p", "--port", type=int, default=8765)
        p.add_argument("--open", action="store_true")
        p.add_argument("--db", default="")
        args, _ = p.parse_known_args(sys.argv[1:])
        cmd_serve(args)
    else:
        from utils.agent_trace.cli import main as cli_main
        cli_main()


if __name__ == "__main__":
    main()
