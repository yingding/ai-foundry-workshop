from dotenv import load_dotenv

from batch_embeddings import ROOT, Settings, clients, ensure_compute
from permissions_setup import configure_permissions


def main() -> None:
    load_dotenv(ROOT / "config" / ".env")
    settings = Settings()
    ml_client, _, authorization_client = clients(settings)
    ensure_compute(settings, ml_client)
    configure_permissions(settings, ml_client, authorization_client)


if __name__ == "__main__":
    main()