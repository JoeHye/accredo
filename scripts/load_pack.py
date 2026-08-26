import os
import sys
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.loader import load_standard_pack

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def main():
    json_path = None
    if len(sys.argv) > 1:
        json_path = sys.argv[1]
    else:
        json_path = os.environ.get("STANDARD_PACK_PATH")

    if not json_path:
        logger.error("No StandardPack JSON path provided. Pass it as a command-line argument or set the STANDARD_PACK_PATH environment variable.")
        sys.exit(1)

    absolute_json_path = os.path.abspath(json_path)
    logger.info(f"Attempting to load StandardPack JSON from absolute path: {absolute_json_path}")

    if not os.path.exists(absolute_json_path):
        logger.error(f"StandardPack JSON file does not exist at absolute path: {absolute_json_path}")
        sys.exit(1)

    app = create_app()
    with app.app_context():
        try:
            loaded_pack = load_standard_pack(absolute_json_path)
            logger.info(f"Successfully loaded StandardPack ID '{loaded_pack.id}' into the database.")
        except Exception as e:
            logger.error(f"Failed to load StandardPack from {absolute_json_path}: {str(e)}", exc_info=True)
            sys.exit(1)

if __name__ == "__main__":
    main()
