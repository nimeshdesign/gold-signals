# Vercel front door

This folder is a tiny Vercel project. It has no code of its own: `vercel.json` forwards every request to the
dashboard running on the Oracle server (`https://140-245-6-23.sslip.io`). The engine, database and login all stay
on Oracle; Vercel only gives the dashboard a `*.vercel.app` address.

Deploy (from this folder):

```bash
npx vercel login
npx vercel deploy --prod --yes
```

Or in the Vercel dashboard: **Add New → Project → import the GitHub repo → Root Directory: `vercel`** → Deploy.

If the Oracle server's address changes, update the `destination` in `vercel.json` and redeploy.
