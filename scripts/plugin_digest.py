"""Print the integrity digest to put in a plugin manifest's runtime.digest.

    python scripts/plugin_digest.py plugins/reference/go-model/sentiment.exe     # process artifact
    python scripts/plugin_digest.py --image uar-plugin-example:1.0.0               # container image id
"""
from __future__ import annotations

import hashlib
import subprocess
import sys

if len(sys.argv) == 3 and sys.argv[1] == "--image":
    print(subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", sys.argv[2]],
                         capture_output=True, text=True, check=True).stdout.strip())
elif len(sys.argv) == 2:
    print("sha256:" + hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())
else:
    sys.exit(__doc__)
