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

**Laptop**: copy the patches to the build Pi.

```
scp ~/Downloads/PiLNK/sterile-image/patches/*.patch pilnk@pilnk-build.local:/home/pilnk/
```

Expect: one line per patch, no errors.

**Pi4 (build card)**:

```
sudo apt update
sudo apt install -y git
git clone https://github.com/slingb1ade/PiLNK.git ~/pilnk
cd ~/pilnk
git config user.name "AJ"
git config user.email "aj@pilnk.io"
git am ~/*.patch
git log --oneline | head -5
```

Expect: `git am` prints "Applying: ..." for each patch with no conflict. The newest commit is the last patch.

Note: first boot skips its self-update on a test build, because the clone is not on branch `main`
(the update would reset it to `origin/main`, which lacks the new scripts). The log says
"test build: update skipped". A shipped image is built from `main` and updates normally.

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
leave Shrink and Compress on, choose a file name like `pilnk-sterile.img.xz` on a disk with room for the full card size.

Expect: "Saved ... card -> pilnk-sterile.img.xz (...), sha256 saved". A `.sha256` file sits next to it.

## 7. Verify

**Laptop**: decompress (keep the `.xz`):

```
xz -dk pilnk-sterile.img.xz
```

Expect: `pilnk-sterile.img` appears next to it.

**Laptop**: run the verifier on the raw image:

```
cd ~/pilnk
sudo bash image/pilnk-image-verify.sh ~/pilnk-sterile.img --denylist ~/.config/pilnk/denylist
```

(Use the path where your clone and image really are. A card in the laptop works too: use `/dev/sdX`, it is opened read-only.)

Expect: it ends with `CLEAN: no rule failures, no raw-image hits`.

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
