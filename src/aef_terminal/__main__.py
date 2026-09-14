from aef_terminal.config import AppConfig


def main() -> None:
    config = AppConfig()
    print(f"MartinCall scaffold: data_root={config.data_root}")


if __name__ == "__main__":
    main()
