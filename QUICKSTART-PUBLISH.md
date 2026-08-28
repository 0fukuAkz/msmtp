# 🚀 Quick Publish Commands

## Fastest Path to Publishing

### 1. **Interactive Publishing** (Recommended)
```bash
./publish.sh
```
This script will guide you through:
- Git initialization
- GitHub repository setup
- PyPI publishing (TestPyPI or production)

---

### 2. **Manual PyPI Publishing**

#### Test First (Recommended)
```bash
# Create TestPyPI account: https://test.pypi.org/account/register/
# Get API token: https://test.pypi.org/manage/account/token/

twine upload --repository testpypi dist/*
# Enter: __token__
# Password: pypi-YOUR_TEST_TOKEN

# Test installation
pip install --index-url https://test.pypi.org/simple/ mercury-smtp
python -c "from mercury_smtp import AsyncSMTPSender; print('✅ Works!')"
```

#### Production Upload
```bash
# Create PyPI account: https://pypi.org/account/register/
# Get API token: https://pypi.org/manage/account/token/

twine upload dist/*
# Enter: __token__
# Password: pypi-YOUR_PRODUCTION_TOKEN
```

---

### 3. **GitHub Repository**

```bash
# Initialize git
git init
git add .
git commit -m "Initial release v1.0.0"

# Create repository on GitHub: https://github.com/new
# Repository name: mercury-smtp

# Push to GitHub
git remote add origin https://github.com/YOUR_USERNAME/mercury-smtp.git
git branch -M main
git push -u origin main

# Create release tag
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0
```

---

## ✅ Package Status

**Built:** ✅ `dist/mercury_smtp-1.0.0-py3-none-any.whl` (23 KB)  
**Built:** ✅ `dist/mercury_smtp-1.0.0.tar.gz` (32 KB)  
**Validated:** ✅ `twine check` passed  
**Tests:** ✅ 8/8 passing  
**Ready:** ✅ Ready to publish!

---

## 📚 Full Documentation

See [PUBLISHING.md](PUBLISHING.md) for complete guide including:
- PyPI account setup
- API token configuration
- TestPyPI testing
- GitHub Actions automation
- Troubleshooting

---

## 🎯 Recommended Workflow

1. **Test on TestPyPI first**
   ```bash
   twine upload --repository testpypi dist/*
   ```

2. **Verify installation**
   ```bash
   pip install --index-url https://test.pypi.org/simple/ mercury-smtp
   python -c "from mercury_smtp import AsyncSMTPSender; print('OK')"
   ```

3. **Publish to production PyPI**
   ```bash
   twine upload dist/*
   ```

4. **Push to GitHub**
   ```bash
   git init && git add . && git commit -m "v1.0.0"
   git remote add origin https://github.com/YOUR_USERNAME/mercury-smtp.git
   git push -u origin main
   ```

5. **Create GitHub release** at:  
   `https://github.com/YOUR_USERNAME/mercury-smtp/releases/new`

---

## 🆘 Troubleshooting

**"Invalid credentials"**
```bash
# Use API token, not password
# Username: __token__
# Password: pypi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**"Package already exists"**
```bash
# Increment version in pyproject.toml
# Then rebuild: rm -rf dist/ && python -m build
```

**"README doesn't render"**
```bash
# Validate: twine check dist/*
# Should show: PASSED
```

---

## 📞 Need Help?

- Full guide: [PUBLISHING.md](PUBLISHING.md)
- PyPI help: https://pypi.org/help/
- Python Packaging: https://packaging.python.org/

**Ready to publish?** Run `./publish.sh` or follow manual steps above.
