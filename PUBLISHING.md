# Publishing Guide for mercury-smtp

## ✅ Pre-Publish Checklist

- [x] Package built successfully (`dist/mercury_smtp-1.0.0.tar.gz` & `.whl`)
- [x] Twine validation passed
- [x] All tests passing (8/8)
- [x] Documentation complete (README.md, Performance.md, CHANGELOG.md)
- [x] License included (MIT)
- [x] CI/CD workflows configured
- [x] .gitignore and MANIFEST.in created

---

## 📦 Option 1: Publish to PyPI (Recommended)

### Step 1: Register PyPI Account
1. Create account at https://pypi.org/account/register/
2. Verify your email address
3. Enable Two-Factor Authentication (recommended)

### Step 2: Create API Token
1. Go to https://pypi.org/manage/account/token/
2. Click "Add API token"
3. Scope: "Entire account" (or project-specific after first upload)
4. Copy the token (starts with `pypi-`)

### Step 3: Configure Credentials
```bash
# Option A: Use keyring (recommended)
pip install keyring
keyring set https://upload.pypi.org/legacy/ __token__

# Option B: Create ~/.pypirc
cat > ~/.pypirc << 'EOF'
[pypi]
username = __token__
password = pypi-YOUR_TOKEN_HERE
EOF
chmod 600 ~/.pypirc
```

### Step 4: Test with TestPyPI (Optional but Recommended)
```bash
# Register at https://test.pypi.org/ and create API token

# Upload to TestPyPI
twine upload --repository testpypi dist/*

# Test installation from TestPyPI
pip install --index-url https://test.pypi.org/simple/ mercury-smtp

# Verify imports work
python -c "from mercury_smtp import AsyncSMTPSender; print('OK')"
```

### Step 5: Publish to PyPI
```bash
# Upload to PyPI (production)
twine upload dist/*

# Verify upload
# Visit: https://pypi.org/project/mercury-smtp/

# Test installation
pip install mercury-smtp
```

---

## 🐙 Option 2: GitHub Repository Setup

### Step 1: Initialize Git
```bash
git init
git add .
git commit -m "Initial release v1.0.0"
```

### Step 2: Create GitHub Repository
1. Go to https://github.com/new
2. Repository name: `mercury-smtp`
3. Description: "Production-grade async SMTP library with pooling & resilience"
4. Public repository
5. **DO NOT** initialize with README (we already have one)
6. Click "Create repository"

### Step 3: Push to GitHub
```bash
# Add remote (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/mercury-smtp.git

# Push code
git branch -M main
git push -u origin main

# Create release tag
git tag -a v1.0.0 -m "Release v1.0.0: Initial production release"
git push origin v1.0.0
```

### Step 4: Configure GitHub Secrets (for automated publishing)
1. Go to repository Settings → Secrets and variables → Actions
2. Click "New repository secret"
3. Name: `PYPI_API_TOKEN`
4. Value: Your PyPI API token
5. Click "Add secret"

### Step 5: Create GitHub Release
1. Go to repository → Releases → "Draft a new release"
2. Tag: `v1.0.0`
3. Title: `v1.0.0 - Initial Production Release`
4. Description: Copy from CHANGELOG.md
5. Click "Publish release"
   - This will automatically trigger the PyPI publish workflow

---

## 🚀 Option 3: Combined Approach (Best Practice)

### Quick Start Script
Run this automated script:

```bash
#!/bin/bash
set -e

echo "🚀 Publishing mercury-smtp v1.0.0"
echo "=================================="

# 1. Git setup
echo "📁 Initializing git repository..."
git init
git add .
git commit -m "Initial release v1.0.0"

# 2. Prompt for GitHub username
read -p "Enter your GitHub username: " github_user
git remote add origin "https://github.com/$github_user/mercury-smtp.git"

# 3. Push to GitHub
echo "📤 Pushing to GitHub..."
git branch -M main
git push -u origin main
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0

# 4. Publish to PyPI
echo "📦 Publishing to PyPI..."
read -p "Upload to PyPI? (y/n): " upload_pypi
if [ "$upload_pypi" = "y" ]; then
    twine upload dist/*
fi

echo "✅ Publication complete!"
echo "PyPI: https://pypi.org/project/mercury-smtp/"
echo "GitHub: https://github.com/$github_user/mercury-smtp"
```

Save as `publish.sh`, then run:
```bash
chmod +x publish.sh
./publish.sh
```

---

## 📋 Post-Publication Checklist

After publishing, update these locations:

### 1. PyPI Project Page
- [ ] Add project logo (via web interface)
- [ ] Verify README renders correctly
- [ ] Check all links work

### 2. GitHub Repository
- [ ] Add topics/tags: `smtp`, `email`, `async`, `python`, `circuit-breaker`
- [ ] Enable GitHub Pages (for documentation)
- [ ] Add shields/badges to README:
  ```markdown
  ![PyPI](https://img.shields.io/pypi/v/mercury-smtp)
  ![Python](https://img.shields.io/pypi/pyversions/mercury-smtp)
  ![License](https://img.shields.io/github/license/YOUR_USERNAME/mercury-smtp)
  ![Tests](https://github.com/YOUR_USERNAME/mercury-smtp/actions/workflows/ci.yml/badge.svg)
  ```

### 3. Documentation
- [ ] Add installation instructions with PyPI link
- [ ] Create Read the Docs account (optional)
- [ ] Add contributing guidelines

### 4. Community
- [ ] Announce on social media / Reddit / Hacker News
- [ ] Share in Python communities
- [ ] Add to awesome-python lists

---

## 🔄 Future Releases

For subsequent releases:

1. **Update version** in `pyproject.toml`
2. **Update CHANGELOG.md** with changes
3. **Commit changes**: `git commit -am "Release vX.Y.Z"`
4. **Create tag**: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`
5. **Push**: `git push && git push --tags`
6. **Build**: `rm -rf dist/ && python -m build`
7. **Publish**: `twine upload dist/*`

Or use GitHub Releases which will auto-publish via workflow.

---

## ⚠️ Important Notes

1. **Package names are permanent** - You cannot delete/rename on PyPI
2. **Test on TestPyPI first** - Catch issues before production
3. **Use API tokens** - Never use passwords directly
4. **Enable 2FA** - Protect your PyPI account
5. **Version carefully** - Follow semantic versioning (MAJOR.MINOR.PATCH)

---

## 🆘 Troubleshooting

### "Package already exists"
- You already uploaded this version. Increment version in `pyproject.toml`

### "Invalid credentials"
- Check your API token is correct
- Verify `~/.pypirc` format
- Try keyring: `keyring set https://upload.pypi.org/legacy/ __token__`

### "README doesn't render"
- Check markdown syntax: `twine check dist/*`
- Ensure UTF-8 encoding
- Validate with: https://readme-renderer.readthedocs.io/

### "Imports fail after install"
- Check `MANIFEST.in` includes all necessary files
- Verify `src/` layout is correct
- Try: `pip install -e .` locally first

---

## 📞 Need Help?

- PyPI Support: https://pypi.org/help/
- Python Packaging Guide: https://packaging.python.org/
- Mercury SMTP Issues: https://github.com/YOUR_USERNAME/mercury-smtp/issues

---

**Status:** ✅ Package is ready to publish!

**Build Location:** `dist/mercury_smtp-1.0.0-py3-none-any.whl` & `.tar.gz`

**Next Action:** Choose Option 1, 2, or 3 above and follow the steps.
