import os
import asyncio
import logging
import humanize
import hashlib
import json
from datetime import datetime
from typing import Optional, Dict, Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from boto3.s3.transfer import TransferConfig
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified
from dotenv import load_dotenv
import aiofiles

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Bot configuration
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Wasabi configuration
WASABI_ACCESS_KEY = os.getenv("WASABI_ACCESS_KEY")
WASABI_SECRET_KEY = os.getenv("WASABI_SECRET_KEY")
WASABI_BUCKET = os.getenv("WASABI_BUCKET")
WASABI_REGION = os.getenv("WASABI_REGION", "us-east-1")

# Optional configurations
STORAGE_CHANNEL_ID = os.getenv("STORAGE_CHANNEL_ID")
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", "4294967296"))  # 4GB

# File storage for metadata
FILES_DB = "files_database.json"

class WasabiStorage:
    def __init__(self):
        self.s3_client = None
        self._initialize_client()

    def _initialize_client(self):
        try:
            if not all([WASABI_ACCESS_KEY, WASABI_SECRET_KEY, WASABI_BUCKET]):
                logger.warning("Wasabi credentials not fully configured")
                return

            clean_region = WASABI_REGION.replace('s3.', '').replace('.wasabisys.com', '')

            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=WASABI_ACCESS_KEY,
                aws_secret_access_key=WASABI_SECRET_KEY,
                endpoint_url=f'https://s3.{clean_region}.wasabisys.com',
                region_name=clean_region
            )
            logger.info("Wasabi S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Wasabi client: {e}")

    async def test_connection(self) -> bool:
        try:
            if not self.s3_client:
                return False

            self.s3_client.head_bucket(Bucket=WASABI_BUCKET)
            return True
        except Exception as e:
            logger.error(f"Wasabi connection test failed: {e}")
            return False

    async def upload_file(self, file_path: str, object_key: str, progress_callback=None) -> bool:
        try:
            if not self.s3_client:
                return False

            file_size = os.path.getsize(file_path)

            def upload_progress(bytes_transferred):
                if progress_callback:
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(progress_callback(bytes_transferred, file_size, "☁️ Uploading to cloud storage..."))
                    except:
                        pass

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                await asyncio.get_event_loop().run_in_executor(
                    executor,
                    lambda: self.s3_client.upload_file(
                        file_path,
                        WASABI_BUCKET,
                        object_key,
                        Callback=upload_progress,
                        Config=TransferConfig(
                            multipart_threshold=1024 * 25,
                            max_concurrency=10,
                            multipart_chunksize=1024 * 25,
                            use_threads=True
                        )
                    )
                )

            logger.info(f"File uploaded successfully: {object_key}")
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False

    async def download_file(self, object_key: str, file_path: str, progress_callback=None) -> bool:
        try:
            if not self.s3_client:
                return False

            response = self.s3_client.head_object(Bucket=WASABI_BUCKET, Key=object_key)
            file_size = response['ContentLength']

            def download_progress(bytes_transferred):
                if progress_callback:
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(progress_callback(bytes_transferred, file_size, "📥 Downloading from cloud..."))
                    except:
                        pass

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                await asyncio.get_event_loop().run_in_executor(
                    executor,
                    lambda: self.s3_client.download_file(
                        WASABI_BUCKET,
                        object_key,
                        file_path,
                        Callback=download_progress,
                        Config=TransferConfig(
                            multipart_threshold=1024 * 25,
                            max_concurrency=10,
                            multipart_chunksize=1024 * 25,
                            use_threads=True
                        )
                    )
                )

            logger.info(f"File downloaded successfully: {object_key}")
            return True
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return False

    def get_presigned_url(self, object_key: str, expiration: int = 3600) -> Optional[str]:
        try:
            if not self.s3_client:
                return None

            url = self.s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': WASABI_BUCKET, 'Key': object_key},
                ExpiresIn=expiration
            )
            return url
        except Exception as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            return None

    async def delete_file(self, object_key: str) -> bool:
        try:
            if not self.s3_client:
                return False

            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                await asyncio.get_event_loop().run_in_executor(
                    executor,
                    lambda: self.s3_client.delete_object(
                        Bucket=WASABI_BUCKET,
                        Key=object_key
                    )
                )

            logger.info(f"File deleted successfully from cloud: {object_key}")
            return True
        except Exception as e:
            logger.error(f"Delete failed: {e}")
            return False

class FileDatabase:
    def __init__(self, db_file: str):
        self.db_file = db_file
        self.files = self._load_database()

    def _load_database(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.db_file):
                with open(self.db_file, 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Failed to load database: {e}")
            return {}

    def _save_database(self):
        try:
            with open(self.db_file, 'w') as f:
                json.dump(self.files, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save database: {e}")

    def add_file(self, file_id: str, file_data: Dict[str, Any]):
        self.files[file_id] = file_data
        self._save_database()

    def get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        return self.files.get(file_id)

    def list_files(self) -> Dict[str, Any]:
        return self.files

    def delete_file(self, file_id: str) -> bool:
        if file_id in self.files:
            del self.files[file_id]
            self._save_database()
            return True
        return False

# Initialize components
storage = WasabiStorage()
file_db = FileDatabase(FILES_DB)

# Initialize Pyrogram client
if not all([API_ID, API_HASH, BOT_TOKEN]):
    logger.error("Missing required Telegram credentials")
    exit(1)

app = Client(
    "file_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=16,
    sleep_threshold=120
)

# Progress callback
async def progress_callback(current: int, total: int, action: str, message: Optional[Message] = None):
    try:
        percentage = (current / total) * 100
        progress_bar = "█" * int(percentage / 10) + "░" * (10 - int(percentage / 10))

        text = f"{action}\n\n"
        text += f"Progress: {percentage:.1f}%\n"
        text += f"[{progress_bar}]\n"
        text += f"{humanize.naturalsize(current)} / {humanize.naturalsize(total)}"

        if message and (not hasattr(progress_callback, 'last_percentage') or percentage - progress_callback.last_percentage >= 5.0 or percentage >= 100):
            progress_callback.last_percentage = percentage
            await message.edit_text(text)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass

# (KEEP YOUR COMMAND HANDLERS SAME AS BEFORE...)

if __name__ == "__main__":
    logger.info("Starting Telegram File Bot...")

    missing_vars = []
    required_vars = ['API_ID', 'API_HASH', 'BOT_TOKEN']

    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)

    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        exit(1)

    if not all([WASABI_ACCESS_KEY, WASABI_SECRET_KEY, WASABI_BUCKET]):
        logger.warning("Wasabi credentials not configured. Cloud storage will be disabled.")

    logger.info("Bot configuration validated. Starting...")
    app.run()
            
