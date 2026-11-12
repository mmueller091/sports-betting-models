"""Entry point. Run with: python run.py [--dev]"""
import sys
import uvicorn

if __name__ == "__main__":
    dev = "--dev" in sys.argv
    uvicorn.run(
        "api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=dev,
        reload_dirs=["C:/Users/mmuel/Documents/Gambling/backend"] if dev else None,
    )
