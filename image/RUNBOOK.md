# PiLNK sterile image: build, check, test

Follow top to bottom. Every block says which machine to type it on.
Nothing here holds a password or key. Use a spare card for the build, never the Pi4's own card.

## 1. Flash the build card

**Laptop**: open Raspberry Pi Imager and choose:

- OS: Raspberry Pi OS Lite (64-bit), Trixie.
- Hostname `pilnk-build`.
- User `pilnk` with a temporary password (you will never reuse it).
- SSH on (password login).
- Network: plug in Ethernet if you can. Wi-Fi set here is fine, because sterilise removes it.

Put the card in the Pi4 and power on.

Expect: `ssh pilnk@pilnk-build.local` asks for the temporary password and logs in.

## 2. Clone and apply the branch

Do this **before "ship it"**. The branch is not on `main` yet, so you get patches
(they will be in `PiLNK/sterile-image/patches/`).

**Laptop**: put the patches in one dedicated directory, `~/sterile-patches/`, and copy that directory to the build Pi:

```
mkdir -p ~/sterile-patches
cp ~/Downloads/PiLNK/sterile-image/patches/*.patch ~/sterile-patches/
scp -r ~/sterile-patches pilnk@pilnk-build.local:/home/pilnk/
```

Expect: one line per patch, no errors. The patches are now in `/home/pilnk/sterile-patches/` on the Pi.

**Pi4 (build card)**:

```
sudo apt update
sudo apt install -y git
git clone https://github.com/slingb1ade/PiLNK.git ~/pilnk
git -C ~/pilnk switch -c sterile-image
git -C ~/pilnk -c user.name=AJ -c user.email=aj@pilnk.io am ~/sterile-patches/*.patch
git -C ~/pilnk log --oneline | head -5
```

Expect: `git am` prints "Applying: ..." for each patch with no conflict. The newest commit is the last patch.
`git -C ~/pilnk branch --show-current` prints `sterile-image`.

Two things matter here:

- **Do the `switch -c` before `git am`.** First boot skips its self-update only when the clone is on a branch
  other than `main`. On `main` it would run the update, which does `git reset --hard origin/main` and throws
  the patches away. On the first boot of a test card the log (`/var/log/pilnk-firstboot.log`) must read
  `test build: update skipped (branch sterile-image)`. If it does not, the card is not running the branch's code.
  A shipped image is built from `main` and updates normally.
- **Never run `git config user.name` or `user.email` in the clone.** The identity above is passed with `-c`
  for this one command only and is not stored. A stored identity would sit in `.git/config` on every card.
  (Sterilise also removes any `[user]` section and its check fails if one is left, as a second guard.)

## 3. Build

**Pi4 (build card)**: unplug every SDR dongle (RTL-SDR and Airspy) first. Then:

```
sudo apt install -y cloud-guest-utils
bash ~/pilnk/image/pilnk-image-build.sh
```

It refuses, and says why, if: the user is not `pilnk`, the OS is not Trixie, it is not 64-bit,
a dongle is plugged in, `growpart` is missing, or it is not run from `~/pilnk`.
It installs PiLNK without starting it, and installs the first-boot and Wi-Fi setup units **disabled**.

Expect: ends with "Build done" and "Next step:  sudo bash image/pilnk-sterilise.sh". PiLNK is not running.

## 4. Denylist (private strings only)

**Laptop**: make a plain text file, one string per line, `#` lines are comments:

```
mkdir -p ~/.config/pilnk
nano ~/.config/pilnk/denylist
```

Put in strings that are private to you: your home Wi-Fi name, your street, your personal email,
your tailnet name. Do **not** put public project names (PiLNK, your GitHub handle): they are in
the public code and in `.git/config` on purpose. No passwords. This file stays on the laptop, never on the Pi, never committed.

Expect: `wc -l ~/.config/pilnk/denylist` shows a few lines.

## 5. Sterilise

**Pi4 (build card)**: from the SSH session:

```
cd ~/pilnk
sudo bash image/pilnk-sterilise.sh
```

Pi OS gives the first user passwordless sudo, so no password prompt. What happens:

- It runs a pre-check first. If that fails, your **session stays open** and it prints where the log is
  (`/run/pilnk-sterilise.log`). Read it, fix, run again.
- If the pre-check passes, it moves itself out of your session. Your SSH session **closes**, then it
  wipes the traces, arms first boot, runs the check again, and **powers the Pi off** within a few minutes.

Expect: the session drops and the Pi's green light stops.

**If the Pi has not powered off after 10 minutes:** pull the power and put the card in the laptop.
**Never boot it again.** On the laptop, read `pilnk-sterilise-failed.txt` on the boot partition (ids and paths only),
and run the verifier (section 7) on the card.

Expect: either the Pi is off, or you have `pilnk-sterilise-failed.txt` telling you which check failed.

## 6. Save the image

**Laptop**: put the card in the laptop, open Inkpot v0.6, choose "Save to image", pick the card,
leave Shrink and Compress on, save it as `~/pilnk-images/pilnk-sterile.img.xz` (make the folder first: `mkdir -p ~/pilnk-images`;
the disk needs room for the full card size).

Expect: "Saved ... card -> pilnk-sterile.img.xz (...), sha256 saved". A `.sha256` file sits next to it.

## 7. Verify

**Laptop**: decompress (keep the `.xz`):

```
cd ~/pilnk-images
xz -dk pilnk-sterile.img.xz
```

Expect: `pilnk-sterile.img` appears next to it.

The verifier runs from a clone of the **branch** on the laptop (it is not on `main` yet, so a plain clone does not have it).
**Laptop**: build that clone from the same patches (skip this if `~/pilnk-verify` already exists):

```
git clone https://github.com/slingb1ade/PiLNK.git ~/pilnk-verify
git -C ~/pilnk-verify switch -c sterile-image
git -C ~/pilnk-verify -c user.name=AJ -c user.email=aj@pilnk.io am ~/sterile-patches/*.patch
```

Expect: "Applying: ..." for each patch, no conflict.

**Laptop**: run the verifier on the raw image:

```
cd ~/pilnk-verify
sudo bash image/pilnk-image-verify.sh ~/pilnk-images/pilnk-sterile.img --denylist ~/.config/pilnk/denylist
```

Expect: it ends with `CLEAN: no rule failures, no raw-image hits`.

To check a card still in the laptop instead of an image file: first find it.

```
lsblk -o NAME,SIZE,MODEL,TRAN
```

Confirm the card's name and size (for example `sdb`, 32G, a USB reader) **before** you type any `/dev/sdX` command.
The system disk is the one holding `/`; never use it. Then use that name in place of `/dev/sdX`
(the verifier opens it read-only):

```
cd ~/pilnk-verify
sudo bash image/pilnk-image-verify.sh /dev/sdX --denylist ~/.config/pilnk/denylist
```

Expect: the same `CLEAN` line.

What the lines mean:

- `CLEAN` line at the end: nothing found. The image is OK to test.
- `HIT <id> at <path>` or `at offset N (free space ...)`: a trace was found. **Do not give the image to anyone.**
  It prints where, never the secret itself. `deny#N` is line N of your denylist. Fix it on the build card
  (or note it) and rebuild.
- `SKIP ... (public PiLNK file)`: a denylist string sits inside an unchanged PiLNK file that is already public. Not a failure.
- `SKIP ... (unmodified Debian package file)`: a key-shaped string inside a stock Debian file. Not a failure.
- The last line `NOT CLEAN` means at least one HIT.

## 8. Test matrix

Write the image to two test cards with Inkpot. Tick each box on **both a Pi 4 and a Pi 5** where it applies.

**Pi4 / Pi5 (test card)**:

- [ ] Pi4, no cable, dongle in: the `PiLNK-Setup-XXXX` hotspot appears (open, no password).
- [ ] Phone joins it and the setup page opens (or browse to `http://10.42.0.1`).
- [ ] A wrong Wi-Fi password: the hotspot comes back with its error message.
- [ ] The right password: the Pi joins your Wi-Fi.
- [ ] The pairing code shows.
- [ ] Claim it with a test account: aircraft come in.
- [ ] Pi5 on a cable: no hotspot, and the pairing code shows.
- [ ] Two cards, two nodes: different hostnames, machine IDs and SSH host keys, and two separate pending nodes on pilnk.io.
- [ ] Power cut mid first boot: pull the power, plug in again, the next boot finishes cleanly.
- [ ] A bigger card than the image: the root partition grew to fill it.

Expect: every box ticked on Pi4 and Pi5, or a note of what failed.

**First-real-run watch list** (things the tests could not prove; look for them on the first boot):

- [ ] Is the hotspot really **open** (no password prompt on the phone)?
- [ ] Switching from hotspot to client: how long until the Pi is on your Wi-Fi? Does a failed join rescan and bring the hotspot back in reasonable time?
- [ ] Does Android / iOS open the setup page by itself? If not, `http://10.42.0.1` still works (note which).
- [ ] Does `pilnk-XXXX.local` resolve on first boot (avahi)? Try it from the laptop.
- [ ] Did `growpart` grow `/dev/root`? `lsblk` and `df -h /` after first boot.
- [ ] After sterilise, are there any shell history or login records on the card? (The verifier covers this, but also look at `wtmpdb` and `last` on a booted test card.)
- [ ] Read the stage-2 log: `sudo cat /var/log/pilnk-firstboot.log` after first boot. Every stage should say done.
- [ ] Any `HIT` inside package files on a stock image (a Debian file with a key-shaped string)? Note the path so it can be added as a known stock file.

Expect: each item answered yes, or written down.

## 9. Clean up

**Laptop**: open pilnk.io and remove the test nodes and the test account claims
(Admin, Nodes, delete). Wipe or reuse the test cards.

Expect: no test nodes left in your node list.
