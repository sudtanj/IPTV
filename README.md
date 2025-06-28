# IPTV Playlist Repository

This repository contains an IPTV playlist in M3U format, along with automation scripts and GitHub Actions for quality control and reporting. This script is for research and personal study purposes only. Do not use for rebroadcasting or commercial purposes.

## Features
- **M3U Playlist**: `index.m3u` with Indonesian and international TV channels, movies, kids, and sports sections.
- **Automated Validation**: GitHub Actions check all stream links daily to ensure they are live and valid.
- **Linting**: Checks for duplicate URLs, missing metadata, and formatting issues.
- **Stats Reporting**: Generates statistics about the playlist (number of channels, groups, etc.).
- **EPG Validation**: Validates EPG (Electronic Program Guide) XML files.
- **Dependabot**: Keeps GitHub Actions dependencies up to date.

## Usage
- **Playlist**: Use `index.m3u` in your IPTV player (e.g., OTT Player, VLC, Kodi).
- **Automation**: All checks run automatically on push and daily via GitHub Actions. See the Actions tab for results.

## Scripts
- `lint_m3u.py`: Lints the playlist for format and metadata issues.
- `validate_streams.py`: Checks that all stream URLs are live and return valid video content.
- `stats_m3u.py`: Reports statistics about the playlist.
- `epg_validate.py`: Validates EPG XML files.

## Contributing
- Please do not rebroadcast or sell IPTV streams from this playlist.
- Open issues or pull requests for suggestions, bug reports, or new channel requests.

---

_This repository is maintained by the community. Automation helps keep the playlist healthy and up to date._
