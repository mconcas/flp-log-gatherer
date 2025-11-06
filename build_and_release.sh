#!/bin/bash
#
# Build and Release Script for flp-log-gatherer
# 
# This script builds the Python package and optionally releases it to PyPI
#
# Usage:
#   ./build_and_release.sh [--upload] [--test-pypi] [--clean]
#
# Options:
#   --upload     Upload to PyPI (requires valid credentials)
#   --test-pypi  Upload to Test PyPI instead of production PyPI
#   --clean      Clean build artifacts before building
#   --help       Show this help message
#

set -e  # Exit on any error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Default options
UPLOAD=false
TEST_PYPI=false
CLEAN=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --upload)
            UPLOAD=true
            shift
            ;;
        --test-pypi)
            TEST_PYPI=true
            shift
            ;;
        --clean)
            CLEAN=true
            shift
            ;;
        --help|-h)
            echo "Build and Release Script for flp-log-gatherer"
            echo ""
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --upload     Upload to PyPI (requires valid credentials)"
            echo "  --test-pypi  Upload to Test PyPI instead of production PyPI"
            echo "  --clean      Clean build artifacts before building"
            echo "  --help       Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                    # Just build the package"
            echo "  $0 --clean           # Clean and build"
            echo "  $0 --upload          # Build and upload to PyPI"
            echo "  $0 --test-pypi       # Build and upload to Test PyPI"
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option $1${NC}"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Function to print colored messages
print_step() {
    echo -e "${BLUE}[STEP]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Function to check if command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check prerequisites
print_step "Checking prerequisites..."

if ! command_exists python3; then
    print_error "python3 is required but not installed"
    exit 1
fi

if ! command_exists pip; then
    print_error "pip is required but not installed"
    exit 1
fi

# Check if build tools are available
if ! python3 -c "import build" 2>/dev/null; then
    print_warning "build module not found, installing..."
    pip install build
fi

if [ "$UPLOAD" = true ]; then
    if ! python3 -c "import twine" 2>/dev/null; then
        print_warning "twine not found, installing..."
        pip install twine
    fi
fi

print_success "Prerequisites checked"

# Clean build artifacts if requested
if [ "$CLEAN" = true ]; then
    print_step "Cleaning build artifacts..."
    rm -rf build/
    rm -rf dist/
    rm -rf *.egg-info/
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    print_success "Build artifacts cleaned"
fi

# Verify project structure
print_step "Verifying project structure..."

if [ ! -f "pyproject.toml" ]; then
    print_error "pyproject.toml not found - are you in the project root?"
    exit 1
fi

if [ ! -f "setup.py" ]; then
    print_warning "setup.py not found, but pyproject.toml exists - this is normal for modern Python packages"
fi

if [ ! -d "src" ]; then
    print_error "src/ directory not found"
    exit 1
fi

print_success "Project structure verified"

# Get package version
print_step "Getting package version..."
VERSION=$(python3 -c "
import tomllib
with open('pyproject.toml', 'rb') as f:
    config = tomllib.load(f)
print(config['project']['version'])
" 2>/dev/null || python3 -c "
import configparser
import sys
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib
    except ImportError:
        print('ERROR: tomli package required for Python < 3.11')
        sys.exit(1)
with open('pyproject.toml', 'rb') as f:
    config = tomllib.load(f)
print(config['project']['version'])
")

if [ -z "$VERSION" ]; then
    print_error "Could not determine package version"
    exit 1
fi

print_success "Package version: $VERSION"

# Build the package
print_step "Building package..."

python3 -m build

if [ $? -ne 0 ]; then
    print_error "Package build failed"
    exit 1
fi

print_success "Package built successfully"

# List built artifacts
print_step "Built artifacts:"
ls -la dist/

# Check if dist files exist
WHEEL_FILE=$(find dist/ -name "*.whl" -type f | head -1)
TARBALL_FILE=$(find dist/ -name "*.tar.gz" -type f | head -1)

if [ -z "$WHEEL_FILE" ] || [ -z "$TARBALL_FILE" ]; then
    print_error "Expected build artifacts not found in dist/"
    exit 1
fi

print_success "Found wheel: $(basename "$WHEEL_FILE")"
print_success "Found tarball: $(basename "$TARBALL_FILE")"

# Upload if requested
if [ "$UPLOAD" = true ]; then
    print_step "Preparing to upload package..."
    
    # Check twine configuration
    if [ "$TEST_PYPI" = true ]; then
        REPO_URL="https://test.pypi.org/legacy/"
        REPO_NAME="testpypi"
        print_warning "Uploading to Test PyPI"
    else
        REPO_URL="https://upload.pypi.org/legacy/"
        REPO_NAME="pypi"
        print_warning "Uploading to Production PyPI"
    fi
    
    # Verify credentials are available
    print_step "Checking PyPI credentials..."
    
    if [ "$TEST_PYPI" = true ]; then
        # Try to use stored credentials for test PyPI
        python3 -m twine check dist/*
        if [ $? -ne 0 ]; then
            print_error "Package check failed"
            exit 1
        fi
        
        print_step "Uploading to Test PyPI..."
        python3 -m twine upload --repository testpypi dist/*
    else
        # Try to use stored credentials for production PyPI
        python3 -m twine check dist/*
        if [ $? -ne 0 ]; then
            print_error "Package check failed"
            exit 1
        fi
        
        print_step "Uploading to PyPI..."
        echo -e "${YELLOW}WARNING: This will upload to production PyPI!${NC}"
        read -p "Are you sure you want to continue? (y/N): " -n 1 -r
        echo
        
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            python3 -m twine upload dist/*
        else
            print_warning "Upload cancelled by user"
            exit 0
        fi
    fi
    
    if [ $? -eq 0 ]; then
        print_success "Package uploaded successfully to $REPO_NAME"
        if [ "$TEST_PYPI" = true ]; then
            echo -e "${BLUE}Install with: pip install -i https://test.pypi.org/simple/ flp-log-gatherer==$VERSION${NC}"
        else
            echo -e "${BLUE}Install with: pip install flp-log-gatherer==$VERSION${NC}"
        fi
    else
        print_error "Upload failed"
        exit 1
    fi
else
    print_step "Build completed - not uploading (use --upload to upload)"
fi

# Final summary
echo ""
echo -e "${GREEN}===========================================${NC}"
echo -e "${GREEN}  BUILD AND RELEASE COMPLETED SUCCESSFULLY${NC}"
echo -e "${GREEN}===========================================${NC}"
echo -e "${BLUE}Package:${NC} flp-log-gatherer"
echo -e "${BLUE}Version:${NC} $VERSION"
echo -e "${BLUE}Wheel:${NC} $(basename "$WHEEL_FILE")"
echo -e "${BLUE}Tarball:${NC} $(basename "$TARBALL_FILE")"

if [ "$UPLOAD" = true ]; then
    echo -e "${BLUE}Uploaded to:${NC} $REPO_NAME"
fi

echo ""
echo -e "${BLUE}Next steps:${NC}"
if [ "$UPLOAD" = false ]; then
    echo "  • Test the package: pip install dist/$(basename "$WHEEL_FILE")"
    echo "  • Upload to Test PyPI: $0 --test-pypi"
    echo "  • Upload to PyPI: $0 --upload"
else
    echo "  • Test installation from PyPI"
    echo "  • Update documentation"
    echo "  • Create release notes"
fi
echo ""