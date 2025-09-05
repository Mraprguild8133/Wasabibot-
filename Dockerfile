# Use official Python runtime as base image
FROM python:3.11-slim

# Set working directory in the container
WORKDIR /app

# Copy requirements file first for better caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Command to run the Python application
CMD ["python", "main.py"]
