from app.services.credentials import generate_encryption_key


def main() -> None:
    print(generate_encryption_key())


if __name__ == "__main__":
    main()
