#!/bin/bash
set -e

echo "🚀 Publishing mercury-smtp v1.0.0"
echo "=================================="
echo ""

# Color codes
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 1. Verify package is built
if [ ! -f "dist/mercury_smtp-1.0.0-py3-none-any.whl" ]; then
    echo "${YELLOW}⚠️  Package not built. Building now...${NC}"
    python -m build
    echo "${GREEN}✅ Package built${NC}"
fi

# 2. Git setup
echo ""
echo "${BLUE}📁 Git Repository Setup${NC}"
echo "----------------------"

if [ ! -d ".git" ]; then
    git init
    git add .
    git commit -m "Initial release v1.0.0"
    echo "${GREEN}✅ Git initialized${NC}"
else
    echo "${YELLOW}⚠️  Git already initialized${NC}"
fi

# 3. GitHub remote
echo ""
read -p "Enter your GitHub username (or press Enter to skip): " github_user

if [ ! -z "$github_user" ]; then
    # Check if remote exists
    if git remote | grep -q "origin"; then
        echo "${YELLOW}⚠️  Remote 'origin' already exists${NC}"
    else
        git remote add origin "https://github.com/$github_user/mercury-smtp.git"
        echo "${GREEN}✅ Remote added${NC}"
    fi
    
    # Push to GitHub
    read -p "Push to GitHub now? (y/n): " push_github
    if [ "$push_github" = "y" ]; then
        git branch -M main
        git push -u origin main || echo "${YELLOW}⚠️  Push failed. You may need to create the repository on GitHub first.${NC}"
        git tag -a v1.0.0 -m "Release v1.0.0: Initial production release"
        git push origin v1.0.0 || echo "${YELLOW}⚠️  Tag push failed${NC}"
        echo "${GREEN}✅ Pushed to GitHub${NC}"
        echo ""
        echo "🔗 Repository: https://github.com/$github_user/mercury-smtp"
    fi
fi

# 4. PyPI publishing
echo ""
echo "${BLUE}📦 PyPI Publishing${NC}"
echo "------------------"
echo ""
echo "Options:"
echo "  1) Publish to TestPyPI (test first - recommended)"
echo "  2) Publish to PyPI (production)"
echo "  3) Skip PyPI publishing"
echo ""
read -p "Select option (1/2/3): " pypi_option

case $pypi_option in
    1)
        echo "${BLUE}📤 Uploading to TestPyPI...${NC}"
        twine upload --repository testpypi dist/*
        echo "${GREEN}✅ Uploaded to TestPyPI${NC}"
        echo ""
        echo "🔗 TestPyPI: https://test.pypi.org/project/mercury-smtp/"
        echo ""
        echo "Test installation with:"
        echo "  pip install --index-url https://test.pypi.org/simple/ mercury-smtp"
        ;;
    2)
        echo "${BLUE}📤 Uploading to PyPI...${NC}"
        twine upload dist/*
        echo "${GREEN}✅ Uploaded to PyPI${NC}"
        echo ""
        echo "🔗 PyPI: https://pypi.org/project/mercury-smtp/"
        echo ""
        echo "Install with:"
        echo "  pip install mercury-smtp"
        ;;
    3)
        echo "${YELLOW}⏭️  Skipping PyPI publishing${NC}"
        ;;
    *)
        echo "${YELLOW}⚠️  Invalid option. Skipping PyPI publishing${NC}"
        ;;
esac

# 5. Summary
echo ""
echo "${GREEN}=================================="
echo "✅ Publication Process Complete!"
echo "==================================${NC}"
echo ""
echo "📋 Next Steps:"
echo ""
if [ ! -z "$github_user" ]; then
    echo "1. Visit GitHub and create a release:"
    echo "   https://github.com/$github_user/mercury-smtp/releases/new"
    echo ""
fi
echo "2. Add badges to README.md:"
echo "   ![PyPI](https://img.shields.io/pypi/v/mercury-smtp)"
echo "   ![Python](https://img.shields.io/pypi/pyversions/mercury-smtp)"
echo ""
echo "3. Announce the release:"
echo "   - Reddit: r/Python"
echo "   - Hacker News"
echo "   - Python community forums"
echo ""
echo "📚 Full guide: See PUBLISHING.md for detailed instructions"
echo ""
