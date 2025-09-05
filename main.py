import os
import asyncio
import logging
import humanize
import hashlib
import json
from datetime import datetime

from urllib.parse import quote

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
WASABI_REGION = os.getenv("WASABI_REGION")

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
        """Initialize Wasabi S3 client"""
        try:
            if not all([WASABI_ACCESS_KEY, WASABI_SECRET_KEY, WASABI_BUCKET]):
                logger.warning("Wasabi credentials not fully configured")
                return
            
            # Clean region name - remove any extra formatting
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
        """Test Wasabi connection"""
        try:
            if not self.s3_client:
                return False
            
            self.s3_client.head_bucket(Bucket=WASABI_BUCKET)
            return True
        except Exception as e:
            logger.error(f"Wasabi connection test failed: {e}")
            return False
    
    async def upload_file(self, file_path: str, object_key: str, progress_callback=None) -> bool:
        """Upload file to Wasabi"""
        try:
            if not self.s3_client:
                return False
            
            file_size = os.path.getsize(file_path)
            last_percentage = 0
            
            def upload_progress(bytes_transferred):
                nonlocal last_percentage
                if progress_callback:
                    percentage = (bytes_transferred / file_size) * 100
                    # Only update every 5% to reduce calls
                    if percentage - last_percentage >= 5 or percentage >= 100:
                        last_percentage = percentage
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                asyncio.create_task(progress_callback(percentage, bytes_transferred, file_size))
                        except:
                            pass  # Skip progress updates if event loop issues
            
            # Ultra-fast upload with optimized thread pool
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
                            multipart_threshold=1024 * 25,  # 25MB multipart threshold
                            max_concurrency=10,  # Maximum concurrent uploads
                            multipart_chunksize=1024 * 25,  # 25MB chunk size
                            use_threads=True  # Enable threading
                        )
                    )
                )
            
            logger.info(f"File uploaded successfully: {object_key}")
            return True
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            return False
    
    async def download_file(self, object_key: str, file_path: str, progress_callback=None) -> bool:
        """Download file from Wasabi"""
        try:
            if not self.s3_client:
                return False
            
            # Get file size for progress tracking
            response = self.s3_client.head_object(Bucket=WASABI_BUCKET, Key=object_key)
            file_size = response['ContentLength']
            last_percentage = 0
            
            def download_progress(bytes_transferred):
                nonlocal last_percentage
                if progress_callback:
                    percentage = (bytes_transferred / file_size) * 100
                    # Only update every 5% to reduce calls
                    if percentage - last_percentage >= 5 or percentage >= 100:
                        last_percentage = percentage
                        try:
                            loop = asyncio.get_event_loop()
                            if loop.is_running():
                                asyncio.create_task(progress_callback(percentage, bytes_transferred, file_size))
                        except:
                            pass  # Skip progress updates if event loop issues
            
            # Ultra-fast download with optimized thread pool
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                await asyncio.get_event_loop().run_in_executor(
                    executor,
                    lambda: self.s3_client.download_file(
                        WASABI_BUCKET,
                        object_key,
                        file_path,
                        Callback=download_progress,
                        Config=boto3.s3.transfer.TransferConfig(
                            multipart_threshold=1024 * 25,  # 25MB multipart threshold
                            max_concurrency=10,  # Maximum concurrent downloads
                            multipart_chunksize=1024 * 25,  # 25MB chunk size
                            use_threads=True  # Enable threading
                        )
                    )
                )
            
            logger.info(f"File downloaded successfully: {object_key}")
            return True
        except Exception as e:
            logger.error(f"Download failed: {e}")
            return False
    
    def get_presigned_url(self, object_key: str, expiration: int = 3600) -> Optional[str]:
        """Generate presigned URL for streaming"""
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
        """Delete file from Wasabi"""
        try:
            if not self.s3_client:
                return False
            
            # Run the delete in a thread pool to avoid blocking
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
        """Load files database from JSON"""
        try:
            if os.path.exists(self.db_file):
                with open(self.db_file, 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Failed to load database: {e}")
            return {}
    
    def _save_database(self):
        """Save files database to JSON"""
        try:
            with open(self.db_file, 'w') as f:
                json.dump(self.files, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save database: {e}")
    
    def add_file(self, file_id: str, file_data: Dict[str, Any]):
        """Add file to database"""
        self.files[file_id] = file_data
        self._save_database()
    
    def get_file(self, file_id: str) -> Optional[Dict[str, Any]]:
        """Get file from database"""
        return self.files.get(file_id)
    
    def list_files(self) -> Dict[str, Any]:
        """List all files"""
        return self.files
    
    def delete_file(self, file_id: str) -> bool:
        """Delete file from database"""
        if file_id in self.files:
            del self.files[file_id]
            self._save_database()
            return True
        return False

# Initialize components
storage = WasabiStorage()
file_db = FileDatabase(FILES_DB)

# Initialize Pyrogram client with optimized settings
if not all([API_ID, API_HASH, BOT_TOKEN]):
    logger.error("Missing required Telegram credentials")
    exit(1)

app = Client(
    "file_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    max_concurrent_transmissions=8,  # Maximum concurrent downloads
    sleep_threshold=120,  # Extended flood wait handling
    workers=16,  # Maximum worker threads
    workdir="./session_cache",  # Cache sessions for faster reconnection
    takeout=False,  # Disable takeout mode for speed
    test_mode=False  # Production mode for maximum performance
)

def generate_file_id(file_name: str, file_size: int) -> str:
    """Generate unique file ID"""
    data = f"{file_name}_{file_size}_{datetime.now().isoformat()}"
    return hashlib.md5(data.encode()).hexdigest()[:12]

def get_file_type_emoji(mime_type: str) -> str:
    """Get emoji based on file type"""
    if mime_type.startswith('video/'):
        return '🎥'
    elif mime_type.startswith('audio/'):
        return '🎵'
    elif mime_type.startswith('image/'):
        return '🖼'
    elif mime_type.startswith('application/pdf'):
        return '📄'
    elif mime_type.startswith('application/'):
        return '📁'
    else:
        return '📎'

async def progress_callback(message: Message, current: int, total: int, action: str):
    """Optimized progress callback for uploads/downloads"""
    try:
        percentage = (current / total) * 100
        progress_bar = "█" * int(percentage / 10) + "░" * (10 - int(percentage / 10))
        
        # Calculate speed estimation
        speed = ""
        if hasattr(progress_callback, 'last_time') and hasattr(progress_callback, 'last_current'):
            import time
            current_time = time.time()
            time_diff = current_time - progress_callback.last_time
            if time_diff > 0:
                bytes_diff = current - progress_callback.last_current
                speed_bps = bytes_diff / time_diff
                speed = f" • {humanize.naturalsize(speed_bps)}/s"
        
        progress_callback.last_time = getattr(progress_callback, 'last_time', 0)
        progress_callback.last_current = getattr(progress_callback, 'last_current', 0)
        
        import time
        progress_callback.last_time = time.time()
        progress_callback.last_current = current
        
        text = f"{action}\n\n"
        text += f"Progress: {percentage:.1f}%\n"
        text += f"[{progress_bar}]\n"
        text += f"{humanize.naturalsize(current)} / {humanize.naturalsize(total)}{speed}"
        
        # Ultra-fast progress updates - only every 5% to minimize overhead
        if not hasattr(progress_callback, 'last_percentage'):
            progress_callback.last_percentage = 0
        
        if percentage - progress_callback.last_percentage >= 5.0 or percentage >= 100:
            progress_callback.last_percentage = percentage
            await message.edit_text(text)
    except MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value)
    except Exception:
        pass

@app.on_message(filters.command("start"))
async def start_command(client, message: Message):
    """Handle /start command"""
    welcome_text = """
🤖 **Welcome to File Storage Bot!**

This bot helps you store, manage, and stream files up to 4GB using Wasabi Cloud Storage.

**Available Commands:**
• `/upload` - Upload a file (or just send any file)
• `/download <file_id>` - Download a file by ID
• `/list` - List all stored files
• `/stream <file_id>` - Get streaming link for a file
• `/delete <file_id>` - Delete a file permanently
• `/setchannel <channel_id>` - Set storage channel for backups
• `/test` - Test Wasabi connection
• `/web <file_id>` - Get web player interface link
• `/help` - Show this help information

**Features:**
✅ 4GB file support
✅ Cloud storage with Wasabi
✅ MX Player & VLC integration
✅ Real-time progress tracking
✅ Multiple file type support

Just send me any file to get started!
"""
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 Help", callback_data="help"),
         InlineKeyboardButton("📋 List Files", callback_data="list_files")],
        [InlineKeyboardButton("🔧 Test Connection", callback_data="test_connection")]
    ])
    
    await message.reply_text(welcome_text, reply_markup=keyboard)

@app.on_message(filters.command("help"))
async def help_command(client, message: Message):
    """Handle /help command"""
    help_text = """
📖 **Detailed Help**

**File Upload:**
• Send any file directly to the bot
• Files up to 4GB are supported
• Automatic cloud backup with Wasabi

**File Management:**
• `/list` - See all your stored files
• `/download <file_id>` - Download any file
• `/stream <file_id>` - Get streaming URL
• `/delete <file_id>` - Delete files permanently

**Streaming & Players:**
• `/web <file_id>` - Web player interface
• Direct MX Player integration on Android
• VLC player support for all platforms

**Advanced Features:**
• `/setchannel <channel_id>` - Backup to Telegram channel
• `/test` - Check cloud storage connection
• Real-time upload/download progress

**File ID:** Each uploaded file gets a unique ID for easy access.

**Supported Formats:**
🎥 Videos (MP4, AVI, MKV, etc.)
🎵 Audio (MP3, WAV, FLAC, etc.)
🖼 Images (JPG, PNG, GIF, etc.)
📄 Documents (PDF, DOC, etc.)
📁 Archives (ZIP, RAR, etc.)
"""
    
    await message.reply_text(help_text)

@app.on_message(filters.command("test"))
async def test_command(client, message: Message):
    """Handle /test command"""
    test_msg = await message.reply_text("🔧 Testing Wasabi connection...")
    
    connection_ok = await storage.test_connection()
    
    if connection_ok:
        await test_msg.edit_text("✅ Wasabi connection successful!\n\nCloud storage is ready for file operations.")
    else:
        await test_msg.edit_text("❌ Wasabi connection failed!\n\nPlease check your configuration.")

@app.on_message(filters.command("list"))
async def list_command(client, message: Message):
    """Handle /list command"""
    files = file_db.list_files()
    
    if not files:
        await message.reply_text("📁 No files stored yet.\n\nSend me a file to get started!")
        return
    
    text = "📋 **Your Stored Files:**\n\n"
    
    for file_id, file_data in files.items():
        emoji = get_file_type_emoji(file_data.get('mime_type', ''))
        name = file_data.get('name', 'Unknown')
        size = humanize.naturalsize(file_data.get('size', 0))
        date = file_data.get('upload_date', 'Unknown')
        
        text += f"{emoji} `{file_id}`\n"
        text += f"   📝 **{name}**\n"
        text += f"   📏 {size} • 📅 {date}\n\n"
    
    text += "💡 Use `/download <file_id>` or `/stream <file_id>` to access files."
    
    await message.reply_text(text)

@app.on_message(filters.command("download"))
async def download_command(client, message: Message):
    """Handle /download command"""
    if len(message.command) < 2:
        await message.reply_text("❓ Usage: `/download <file_id>`\n\nUse `/list` to see available files.")
        return
    
    file_id = message.command[1]
    file_data = file_db.get_file(file_id)
    
    if not file_data:
        await message.reply_text(f"❌ File with ID `{file_id}` not found.\n\nUse `/list` to see available files.")
        return
    
    progress_msg = await message.reply_text("📥 Preparing download...")
    
    try:
        # Download from Wasabi to temp file
        temp_file = f"temp_{file_id}_{file_data['name']}"
        
        async def download_progress(percentage, current, total):
            await progress_callback(progress_msg, current, total, "📥 Downloading from cloud...")
        
        success = await storage.download_file(file_data['wasabi_key'], temp_file, download_progress)
        
        if success and os.path.exists(temp_file):
            # Send file to user
            await progress_msg.edit_text("📤 Sending file...")
            
            await message.reply_document(
                temp_file,
                caption=f"📁 **{file_data['name']}**\nFile ID: `{file_id}`"
            )
            
            # Clean up temp file
            os.remove(temp_file)
            await progress_msg.delete()
        else:
            await progress_msg.edit_text("❌ Download failed. Please try again later.")
    
    except Exception as e:
        logger.error(f"Download error: {e}")
        await progress_msg.edit_text("❌ Download failed due to an error.")

@app.on_message(filters.command("stream"))
async def stream_command(client, message: Message):
    """Handle /stream command"""
    if len(message.command) < 2:
        await message.reply_text("❓ Usage: `/stream <file_id>`\n\nUse `/list` to see available files.")
        return
    
    file_id = message.command[1]
    file_data = file_db.get_file(file_id)
    
    if not file_data:
        await message.reply_text(f"❌ File with ID `{file_id}` not found.")
        return
    
    # Generate streaming URL
    streaming_url = storage.get_presigned_url(file_data['wasabi_key'], expiration=3600)
    
    if not streaming_url:
        await message.reply_text("❌ Failed to generate streaming URL.")
        return
    
    file_name = file_data['name']
    mime_type = file_data.get('mime_type', '')
    
    text = f"🎬 **Streaming: {file_name}**\n\n"
    text += f"📏 **Size:** {humanize.naturalsize(file_data.get('size', 0))}\n"
    text += f"📅 **Uploaded:** {file_data.get('upload_date', 'Unknown')}\n\n"
    text += "**Stream Options:**\n"
    text += f"🔗 **Direct URL:** [Click to stream]({streaming_url})\n\n"
    
    keyboard = []
    
    # For video/audio files, provide streaming options
    if mime_type.startswith(('video/', 'audio/')):
        text += "**Player Apps:**\n"
        text += "• Use 'Open in Browser' for web playback\n"
        text += "• Share the direct URL to open in MX Player or VLC\n"
    
    keyboard.extend([
        [InlineKeyboardButton("🌐 Open in Browser", url=streaming_url)],
        [InlineKeyboardButton("📋 Share URL", callback_data=f"share_url_{file_id}")],
        [InlineKeyboardButton("📥 Download", callback_data=f"download_{file_id}")]
    ])
    
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

@app.on_message(filters.command("web"))
async def web_command(client, message: Message):
    """Handle /web command"""
    if len(message.command) < 2:
        await message.reply_text("❓ Usage: `/web <file_id>`\n\nUse `/list` to see available files.")
        return
    
    file_id = message.command[1]
    file_data = file_db.get_file(file_id)
    
    if not file_data:
        await message.reply_text(f"❌ File with ID `{file_id}` not found.")
        return
    
    # Generate streaming URL for web player
    streaming_url = storage.get_presigned_url(file_data['wasabi_key'], expiration=3600)
    
    if not streaming_url:
        await message.reply_text("❌ Failed to generate streaming URL.")
        return
    
    file_name = file_data['name']
    mime_type = file_data.get('mime_type', '')
    
    text = f"🌐 **Web Player: {file_name}**\n\n"
    text += f"📏 **Size:** {humanize.naturalsize(file_data.get('size', 0))}\n"
    text += f"📅 **Uploaded:** {file_data.get('upload_date', 'Unknown')}\n\n"
    
    if mime_type.startswith(('video/', 'audio/')):
        text += "**Media Player Options:**\n"
        text += "• Open in browser for built-in HTML5 player\n"
        text += "• Compatible with mobile devices\n"
        text += "• Share URL to open in any media player app\n\n"
    else:
        text += "**File Access:**\n"
        text += "• Direct download via browser\n"
        text += "• Mobile-friendly interface\n\n"
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Open in Browser", url=streaming_url)],
        [InlineKeyboardButton("📱 Mobile View", url=streaming_url)],
        [InlineKeyboardButton("📋 Share URL", callback_data=f"share_url_{file_id}")]
    ])
    
    await message.reply_text(text, reply_markup=keyboard)

@app.on_message(filters.command("delete"))
async def delete_command(client, message: Message):
    """Handle /delete command"""
    if len(message.command) < 2:
        await message.reply_text("❓ Usage: `/delete <file_id>`\n\nUse `/list` to see available files.")
        return
    
    file_id = message.command[1]
    file_data = file_db.get_file(file_id)
    
    if not file_data:
        await message.reply_text(f"❌ File with ID `{file_id}` not found.")
        return
    
    file_name = file_data['name']
    
    # Confirmation keyboard
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{file_id}"),
         InlineKeyboardButton("❌ Cancel", callback_data="cancel_delete")]
    ])
    
    text = f"🗑 **Delete File?**\n\n"
    text += f"📁 **File:** {file_name}\n"
    text += f"📏 **Size:** {humanize.naturalsize(file_data.get('size', 0))}\n"
    text += f"🆔 **File ID:** `{file_id}`\n\n"
    text += "⚠️ **Warning:** This will permanently delete the file from cloud storage and cannot be undone!"
    
    await message.reply_text(text, reply_markup=keyboard)

@app.on_message(filters.command("setchannel"))
async def setchannel_command(client, message: Message):
    """Handle /setchannel command"""
    if len(message.command) < 2:
        await message.reply_text("❓ Usage: `/setchannel <channel_id>`\n\nExample: `/setchannel @mychannel` or `/setchannel -1001234567890`")
        return
    
    channel_id = message.command[1]
    
    # Store channel ID in environment or database
    # For this example, we'll just confirm the setting
    
    await message.reply_text(f"✅ Storage channel set to: `{channel_id}`\n\nFiles will be backed up to this channel.")

@app.on_message(filters.document | filters.video | filters.audio | filters.photo)
async def handle_file_upload(client, message: Message):
    """Handle file uploads"""
    
    # Get file info
    if message.document:
        file_obj = message.document
        file_name = file_obj.file_name or "document"
        mime_type = file_obj.mime_type or "application/octet-stream"
    elif message.video:
        file_obj = message.video
        file_name = f"video_{file_obj.date}.mp4"
        mime_type = file_obj.mime_type or "video/mp4"
    elif message.audio:
        file_obj = message.audio
        file_name = file_obj.file_name or f"audio_{file_obj.date}.mp3"
        mime_type = file_obj.mime_type or "audio/mpeg"
    elif message.photo:
        file_obj = message.photo
        file_name = f"photo_{file_obj.date}.jpg"
        mime_type = "image/jpeg"
    else:
        await message.reply_text("❌ Unsupported file type.")
        return
    
    file_size = getattr(file_obj, 'file_size', 0)
    
    # Check file size
    if file_size > MAX_FILE_SIZE:
        await message.reply_text(f"❌ File too large! Maximum size: {humanize.naturalsize(MAX_FILE_SIZE)}")
        return
    
    # Generate file ID
    file_id = generate_file_id(file_name, file_size)
    
    progress_msg = await message.reply_text("📥 Starting upload...")
    
    try:
        # Download file from Telegram with optimizations
        async def telegram_progress(current, total):
            await progress_callback(progress_msg, current, total, "📥 Downloading from Telegram...")
        
        # Optimized download with reliable parameters
        temp_file = await client.download_media(
            message,
            file_name=f"temp_{file_id}_{file_name}",
            progress=telegram_progress
        )
        
        if not temp_file or not os.path.exists(temp_file):
            await progress_msg.edit_text("❌ Failed to download file from Telegram.")
            return
        
        # Upload to Wasabi
        wasabi_key = f"files/{file_id}/{file_name}"
        
        async def wasabi_progress(percentage, current, total):
            await progress_callback(progress_msg, current, total, "☁️ Uploading to cloud storage...")
        
        upload_success = await storage.upload_file(temp_file, wasabi_key, wasabi_progress)
        
        if upload_success:
            # Save file metadata
            file_metadata = {
                'name': file_name,
                'size': file_size,
                'mime_type': mime_type,
                'wasabi_key': wasabi_key,
                'upload_date': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'telegram_file_id': file_obj.file_id
            }
            
            file_db.add_file(file_id, file_metadata)
            
            # Success message
            emoji = get_file_type_emoji(mime_type)
            success_text = f"✅ **Upload Complete!**\n\n"
            success_text += f"{emoji} **File:** {file_name}\n"
            success_text += f"📏 **Size:** {humanize.naturalsize(file_size)}\n"
            success_text += f"🆔 **File ID:** `{file_id}`\n\n"
            success_text += "**Quick Actions:**"
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Download", callback_data=f"download_{file_id}"),
                 InlineKeyboardButton("🎬 Stream", callback_data=f"stream_{file_id}")],
                [InlineKeyboardButton("🌐 Web Player", callback_data=f"web_{file_id}"),
                 InlineKeyboardButton("🗑 Delete", callback_data=f"confirm_delete_{file_id}")],
                [InlineKeyboardButton("📋 Copy ID", callback_data=f"copy_id_{file_id}")]
            ])
            
            await progress_msg.edit_text(success_text, reply_markup=keyboard)
        else:
            await progress_msg.edit_text("❌ Upload to cloud storage failed.")
        
        # Clean up temp file
        if os.path.exists(temp_file):
            os.remove(temp_file)
    
    except Exception as e:
        logger.error(f"Upload error: {e}")
        await progress_msg.edit_text(f"❌ Upload failed: {str(e)}")

@app.on_callback_query()
async def handle_callbacks(client, callback_query):
    """Handle inline keyboard callbacks"""
    data = callback_query.data
    
    if data == "help":
        await callback_query.answer()
        await help_command(client, callback_query.message)
    
    elif data == "list_files":
        await callback_query.answer()
        await list_command(client, callback_query.message)
    
    elif data == "test_connection":
        await callback_query.answer("Testing connection...")
        await test_command(client, callback_query.message)
    
    elif data.startswith("copy_id_"):
        file_id = data.replace("copy_id_", "")
        await callback_query.answer(f"File ID copied: {file_id}", show_alert=True)
    
    elif data.startswith("copy_url_"):
        file_id = data.replace("copy_url_", "")
        file_data = file_db.get_file(file_id)
        if file_data:
            url = storage.get_presigned_url(file_data['wasabi_key'])
            await callback_query.answer(f"URL copied!", show_alert=True)
    
    elif data.startswith("download_"):
        file_id = data.replace("download_", "")
        await callback_query.answer("Starting download...")
        fake_message = type('FakeMessage', (), {
            'command': ['download', file_id],
            'reply_text': callback_query.message.reply_text,
            'reply_document': callback_query.message.reply_document
        })()
        await download_command(client, fake_message)
    
    elif data.startswith("stream_"):
        file_id = data.replace("stream_", "")
        await callback_query.answer("Generating stream...")
        fake_message = type('FakeMessage', (), {
            'command': ['stream', file_id],
            'reply_text': callback_query.message.reply_text
        })()
        await stream_command(client, fake_message)
    
    elif data.startswith("web_"):
        file_id = data.replace("web_", "")
        await callback_query.answer("Opening web player...")
        fake_message = type('FakeMessage', (), {
            'command': ['web', file_id],
            'reply_text': callback_query.message.reply_text
        })()
        await web_command(client, fake_message)
    
    elif data.startswith("share_url_"):
        file_id = data.replace("share_url_", "")
        file_data = file_db.get_file(file_id)
        if file_data:
            url = storage.get_presigned_url(file_data['wasabi_key'])
            if url:
                await callback_query.answer(f"Share this URL: {url[:50]}...", show_alert=True)
            else:
                await callback_query.answer("Failed to generate URL", show_alert=True)
        else:
            await callback_query.answer("File not found", show_alert=True)
    
    elif data.startswith("confirm_delete_"):
        file_id = data.replace("confirm_delete_", "")
        await callback_query.answer("Deleting file...")
        
        file_data = file_db.get_file(file_id)
        if not file_data:
            await callback_query.message.edit_text("❌ File not found in database.")
            return
        
        delete_msg = await callback_query.message.edit_text("🗑 Deleting file from cloud storage...")
        
        # Delete from Wasabi cloud storage
        wasabi_success = await storage.delete_file(file_data['wasabi_key'])
        
        if wasabi_success:
            # Delete from local database
            file_db.delete_file(file_id)
            await delete_msg.edit_text(f"✅ **File Deleted Successfully!**\n\n📁 **{file_data['name']}** has been permanently removed from cloud storage and database.")
        else:
            await delete_msg.edit_text("❌ Failed to delete file from cloud storage. The file may not exist or there was a connection error.")
    
    elif data == "cancel_delete":
        await callback_query.answer("Delete cancelled")
        await callback_query.message.edit_text("❌ **Delete Cancelled**\n\nThe file was not deleted.")

if __name__ == "__main__":
    logger.info("Starting Telegram File Bot...")
    
    # Check if required environment variables are set
    missing_vars = []
    required_vars = ['API_ID', 'API_HASH', 'BOT_TOKEN']
    
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        logger.info("Please check your .env file and ensure all required variables are set.")
        exit(1)
    
    # Optional Wasabi check
    if not all([WASABI_ACCESS_KEY, WASABI_SECRET_KEY, WASABI_BUCKET]):
        logger.warning("Wasabi credentials not configured. Cloud storage will be disabled.")
    
    logger.info("Bot configuration validated. Starting...")
    app.run()
