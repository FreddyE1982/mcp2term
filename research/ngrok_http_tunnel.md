# ngrok HTTP Tunnel Quick Reference

- Source: https://ngrok.com/docs (Secure Tunnels – ngrok agent setup)
- Install the ngrok agent from https://ngrok.com/download; Linux users can curl the installer script `curl -s https://ngrok-agent.s3.amazonaws.com/ngrok.asc | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null` then add the apt repo and `sudo apt install ngrok`.
- After installation authenticate the agent using the account token: `ngrok config add-authtoken <token>`.
- Launch an HTTPS tunnel for a local HTTP service using `ngrok http 8000` (tunnel defaults to port 8000, adjust as needed). The command prints both `https://` and `http://` forwarding URLs.
- You can also point to a specific host/port: `ngrok http --url=your-subdomain.ngrok.app 127.0.0.1:8000` when the plan allows custom domains.
- Inspect active tunnels and logs at `http://127.0.0.1:4040`.
- Stop the tunnel with `Ctrl+C` in the ngrok terminal or `ngrok tunnel stop --all` from another shell.
