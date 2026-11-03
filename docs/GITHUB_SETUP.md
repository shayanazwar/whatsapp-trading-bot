# GitHub setup

```bash
git init
git add .
git commit -m "Initial KRONOS-MEXC-INTELLIGENCE release"
git branch -M main
git remote add origin https://github.com/YOUR_ACCOUNT/kronos-mexc-intelligence.git
git push -u origin main
```

Create the destination as a **private** repository first. Do not reuse an existing repository. Confirm `.env`, model weights, DB files, caches and artifacts are excluded by `.gitignore` before committing.
