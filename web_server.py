from flask import Flask, render_template, request, jsonify, redirect, url_for, send_file
import os
import json
from datetime import datetime
import humanize
import boto3
from botocore.exceptions import ClientError
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
WASABI_ACCESS_KEY = os.getenv("WASABI_ACCESS_KEY")
WASABI_SECRET_KEY = os.getenv("WASABI_SECRET_KEY")
WASABI_BUCKET = os.getenv("WASABI_BUCKET")
WASABI_REGION = os.getenv("WASABI_REGION", "us-east-1")

FILES_DB = "files_database.json"

class WebFileManager:
    def __init__(self):
        self.s3_client = None
        self._initialize_s3()
    
    def _initialize_s3(self):
        """Initialize Wasabi S3 client"""
        try:
            if not all([WASABI_ACCESS_KEY, WASABI_SECRET_KEY, WASABI_BUCKET]):
                logger.warning("Wasabi credentials not configured")
                return
            
            clean_region = WASABI_REGION.replace('s3.', '').replace('.wasabisys.com', '')
            
            self.s3_client = boto3.client(
                's3',
                aws_access_key_id=WASABI_ACCESS_KEY,
                aws_secret_access_key=WASABI_SECRET_KEY,
                endpoint_url=f'https://s3.{clean_region}.wasabisys.com',
                region_name=clean_region
            )
            logger.info("Web file manager S3 client initialized")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
    
    def get_presigned_url(self, object_key: str, expiration: int = 3600) -> str:
        """Generate presigned URL for file access"""
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
    
    def load_files_db(self):
        """Load files database"""
        try:
            if os.path.exists(FILES_DB):
                with open(FILES_DB, 'r') as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logger.error(f"Failed to load files database: {e}")
            return {}

# Initialize Flask app
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)

# Initialize file manager
file_manager = WebFileManager()

@app.route('/')
def index():
    """Main page showing all files"""
    files = file_manager.load_files_db()
    
    # Sort files by upload date (newest first)
    sorted_files = []
    for file_id, file_data in files.items():
        file_data['file_id'] = file_id
        sorted_files.append(file_data)
    
    sorted_files.sort(key=lambda x: x.get('upload_date', ''), reverse=True)
    
    return render_template('index.html', files=sorted_files)

@app.route('/player/<file_id>')
def player(file_id):
    """File player page"""
    files = file_manager.load_files_db()
    file_data = files.get(file_id)
    
    if not file_data:
        return "File not found", 404
    
    # Generate streaming URL
    streaming_url = file_manager.get_presigned_url(file_data['wasabi_key'], expiration=7200)
    
    if not streaming_url:
        return "Failed to generate streaming URL", 500
    
    file_data['file_id'] = file_id
    file_data['streaming_url'] = streaming_url
    
    return render_template('player.html', file=file_data)

@app.route('/api/files')
def api_files():
    """API endpoint for file list"""
    files = file_manager.load_files_db()
    
    file_list = []
    for file_id, file_data in files.items():
        file_list.append({
            'id': file_id,
            'name': file_data.get('name', 'Unknown'),
            'size': file_data.get('size', 0),
            'size_human': humanize.naturalsize(file_data.get('size', 0)),
            'mime_type': file_data.get('mime_type', ''),
            'upload_date': file_data.get('upload_date', ''),
            'streaming_url': file_manager.get_presigned_url(file_data['wasabi_key'])
        })
    
    return jsonify(file_list)

@app.route('/api/stream/<file_id>')
def api_stream(file_id):
    """API endpoint for streaming URL"""
    files = file_manager.load_files_db()
    file_data = files.get(file_id)
    
    if not file_data:
        return jsonify({'error': 'File not found'}), 404
    
    streaming_url = file_manager.get_presigned_url(file_data['wasabi_key'])
    
    if not streaming_url:
        return jsonify({'error': 'Failed to generate streaming URL'}), 500
    
    return jsonify({
        'file_id': file_id,
        'name': file_data.get('name'),
        'streaming_url': streaming_url,
        'mime_type': file_data.get('mime_type', ''),
        'size': file_data.get('size', 0)
    })

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'files_count': len(file_manager.load_files_db())
    })

# Error handlers
@app.errorhandler(404)
def not_found(error):
    return render_template('error.html', 
                         error_code=404, 
                         error_message="Page not found"), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('error.html', 
                         error_code=500, 
                         error_message="Internal server error"), 500

if __name__ == '__main__':
    logger.info("Starting web server on port 5000...")
    app.run(host='0.0.0.0', port=5000, debug=False)