import os
import sys
from pathlib import Path

# Add project root to Python module search path
root = Path(__file__).resolve().parent.parent
if str(root) not in sys.path:
    sys.path.insert(0, str(root))

from app.main import app

# Expose 'app' for Vercel Serverless Function
