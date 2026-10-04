# Infrastructure

The system runs unattended on a single small AWS instance. Real account IDs,
IP addresses, key names and bucket names are kept out of this repository.

## AWS (ap-south-1, Mumbai)
- **EC2:** t4g.small (2 vCPU Graviton, 2 GB), Ubuntu Server 24.04 LTS arm64, 30 GiB gp3, termination protection on
- **Elastic IP:** static address, registered with the broker (SEBI's retail-algo rules require a whitelisted static IP for API orders)
- **Access:** ED25519 key pair; SSH (port 22) allowed from one home IP only
- **Storage:** versioned S3 bucket with lifecycle rules (Standard-IA at 90 days, Glacier IR at 365 days)
- **IAM:** instance role with ListBucket + Get/PutObject on that bucket only (no delete), so nothing on the box can destroy a backup
- **Billing:** budget alarm at 80% of a monthly cap

## Server
- 2 GB swap, UTC clock, unattended-upgrades
- Docker CE + Compose, log rotation 10 MB x 3
- AWS CLI v2
- systemd services and timers in `deploy/` (collector, API, publisher, signal, scoring, day-trade journal, post-mortem, backup)

## Configuration
- Broker and Telegram credentials: `/home/ubuntu/.env` (never committed; see the README for the keys)
- Backup bucket: `BACKUP_BUCKET=s3://...` in the same `.env`
- Firebase: copy `firebase/.firebaserc.example` to `firebase/.firebaserc` and fill in your project and site
