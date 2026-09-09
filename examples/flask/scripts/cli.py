import argparse

from example_web.service import answer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--value", type=int, default=0)
    args = parser.parse_args()
    print(answer(args.value))


if __name__ == "__main__":
    main()
