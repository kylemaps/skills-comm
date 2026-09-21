#!/bin/sh
# What options do the Neurodesk container wrappers actually pass to apptainer?
#
#     sh container_opts_check.sh                    # the tools our skills route to
#     sh container_opts_check.sh --all              # every container on CVMFS
#     TOOLS="fsl afni" sh container_opts_check.sh
#
# WHY THIS IS A CHECK AND NOT A NOTE
# Every neuroimaging "binary" on PATH here is a bash wrapper doing
# `singularity exec ... <image>.simg <tool> "$@"`. The fsl wrapper passes a plain
# read-only SIF exec with no --overlay, and that matters: under apptainer's userns
# mode a read-only exec works while every --overlay invocation fails, silently.
#
# THE FAILURE SHAPE IS WHY THIS EXISTS. If one of six containers ships a wrapper
# with --overlay and the pool runs userns, that one tool breaks and the others do
# not. In a benchmark that does not look like infrastructure failing. It looks like
# SynthStrip underperforming BET. Our output is a measurement, so a broken
# dependency biases the number instead of stopping the run -- the same shape as a
# blocked egress host, and the reason five entries in INFRA_LEDGER.md changed a
# published figure.
#
# So this is meant to run in preflight and in the pool's smoke test, where a future
# Neurodesk image change that introduces --overlay is caught as an infrastructure
# fault on the day it lands, rather than inferred from a tool's pass rate months
# later.
#
# Raised by cluster-prod, 2026-09-18, against my own claim -- I had read one wrapper
# and reported it as a property of the workload.
set -u

ROOT="${CVMFS_ROOT:-/cvmfs/neurodesk.ardc.edu.au}/containers"
# The methods the brain-extraction skill can route to. Not every container on CVMFS:
# a --overlay in a tool nothing invokes is not our problem, and scanning ~200
# containers turns a check into a report nobody reads.
TOOLS="${TOOLS:-fsl afni ants hdbet freesurfer synthstrip}"
[ "${1:-}" = "--all" ] && TOOLS=""

# Options that change apptainer's privilege or mount behaviour. --overlay is the one
# with the silent-failure history; the others are here because they would equally
# mean "this wrapper needs more than a read-only exec" and we would want to know.
RISKY='--overlay|--writable|--fakeroot|--nv|--rocm'

[ -d "$ROOT" ] || { echo "SKIP: no $ROOT (not on a machine with CVMFS)"; exit 0; }

echo "=== container wrapper options under $ROOT"

# The variable the wrappers interpolate, unquoted, and never set themselves. It is
# where --overlay can enter without appearing in any wrapper text, and it is
# site-configurable -- empty on one machine, something else on another. Checking the
# files without checking this would be a confident answer to the wrong question.
printf 'neurodesk_singularity_opts = [%s]\n' "${neurodesk_singularity_opts:-UNSET}"
if [ -n "${neurodesk_singularity_opts:-}" ]; then
  echo "  !! set. Every wrapper splices this into its apptainer command line."
  printf '%s' "$neurodesk_singularity_opts" | grep -Eq -- "$RISKY" \
    && echo "  !! and it contains a privilege-changing option" || true
fi

# UNSET above is weaker evidence than it looks, twice over.
#
# First, this script sees the variable only if it was EXPORTED. Set as a plain shell
# variable in a login file, it is invisible here and still spliced into the wrapper,
# because the wrapper runs in the same shell. So look for where it is assigned, not
# only for whether we inherited it.
#
# Second, and this is the reason the file grep further down cannot stand alone: the
# option can arrive at runtime from the environment without appearing in any wrapper
# on CVMFS. A grep over every wrapper returning zero hits would read as proof and be
# nothing of the kind. cluster-prod calls this the pattern where the evidence is
# absent from the first field you would check, and counts this as its cleanest
# instance -- the misleading evidence does not merely fail to appear, it actively
# reads as confirmation.
echo "  where it could be assigned:"
grep -rsn "neurodesk_singularity_opts" \
     /etc/profile.d/ /etc/bash.bashrc "$HOME/.bashrc" "$HOME/.profile" \
     "$HOME/bench/.env" 2>/dev/null | grep -v "^Binary" | head -5 \
  || true
grep -rqs "neurodesk_singularity_opts" /etc/profile.d/ "$HOME/.bashrc" 2>/dev/null \
  || echo "       (no assignment found in the usual login files)"

if [ -n "$TOOLS" ]; then
  dirs=""
  for t in $TOOLS; do
    # Container dirs are <tool>_<version>_<date>. Match the tool name up to the
    # first underscore so `ants` does not also match `antspynet`.
    #
    # NEWEST VERSION ONLY. The first version globbed every version, and CVMFS
    # carries dozens per tool -- `fsl_*` alone is many directories of ~1500
    # generated wrapper scripts each. Grepping all of them is tens of thousands of
    # cold network reads and the check appears to hang. Versions matter for what
    # the agents run, but the question here is whether the WRAPPER GENERATOR emits
    # --overlay, which is a property of the image build and not of the version.
    # Override with VERSIONS=all to sweep the lot if that assumption ever needs
    # testing.
    if [ "${VERSIONS:-newest}" = all ]; then
      for d in "$ROOT"/"$t"_*/; do [ -d "$d" ] && dirs="$dirs $d"; done
    else
      # Sort on the trailing _YYYYMMDD, not on the whole name. A plain sort is
      # LEXICAL, so fsl_6.0.7.8_20240913 sorts after fsl_6.0.7.22_20260416 -- "8"
      # beats "2" as a character. The first run of this on real CVMFS duly checked
      # a 2024 image while we pin 6.0.7.22, and reported "ok" about a container no
      # agent has ever used. The date field is fixed-width and sorts correctly.
      d=$(ls -d "$ROOT"/"$t"_*/ 2>/dev/null \
          | sed 's|/$||' | awk -F_ '{print $NF, $0}' | sort | tail -1 | cut -d' ' -f2-)
      [ -n "$d" ] && [ -d "$d" ] && dirs="$dirs $d"
    fi
  done
else
  dirs=$(find "$ROOT" -maxdepth 1 -type d ! -path "$ROOT")
fi

[ -n "${dirs# }" ] || { echo "FAIL: no container directories matched: $TOOLS"; exit 1; }

found=0
for d in $dirs; do
  name=$(basename "$d")
  # SAMPLE, do not sweep. "They are small text files" was true and irrelevant:
  # there are ~1500 per container and they live on CVMFS, so a full grep is
  # thousands of cold network reads per directory and the check looks hung.
  #
  # The wrappers are GENERATED -- identical but for the tool name and the image
  # path -- so N of them answer the same question as all of them. Sampling is
  # stated rather than hidden, because it is the one assumption that could make
  # this miss a real --overlay: a generator that special-cases a single command.
  # SAMPLE=all to check every wrapper when that matters.
  # ${d%/}/ normalises the separator. The two ways $d is produced disagree about
  # the trailing slash -- the glob keeps it, the newest-version selection strips it
  # -- and "$d" alone built paths like .../hdbet_1_xhd-bet. grep then matched
  # nothing and every container reported "ok". A check that silently reports clean
  # is the worst failure this one could have, and only the negative control caught
  # it: the fixture with a known --overlay stopped failing.
  if [ "${SAMPLE:-12}" = all ]; then
    cand=$(ls "${d%/}" 2>/dev/null | sed "s|^|${d%/}/|")
  else
    cand=$(ls "${d%/}" 2>/dev/null | head -"${SAMPLE:-12}" | sed "s|^|${d%/}/|")
  fi
  # A COMMAND WRAPPER, not everything in the directory. These dirs also hold the
  # installer, activate/deactivate scripts, an uninstaller, README, LICENSE and
  # commands.txt -- and the installer legitimately mentions options the wrappers
  # never pass. On the real CVMFS this reported synthstrip as passing --nv; the hit
  # was run_transparent_singularity.sh, and the actual mri_synthstrip wrapper is a
  # plain read-only exec identical to fsl's. A false positive on the one tool whose
  # behaviour the whole benchmark's mechanism claim rests on.
  #
  # A wrapper is defined by what it does: exec the SIF. That is the file whose
  # options reach apptainer at run time, and nothing else in the directory does.
  # Two filters, because neither alone is enough. The installer GENERATES wrappers,
  # so it contains a `singularity exec` template and passes the content test; and a
  # name test alone would break the moment a container ships a differently-named
  # helper. Exclude the known non-wrappers by name, then require the exec line.
  cand=$(printf '%s\n' $cand | grep -vE \
    '/(run_transparent_singularity\.sh|ts_[^/]*|activate_[^/]*|deactivate_[^/]*|LICENSE|README\.md|[^/]*\.(txt|md|simg|json)|manual_module_files)$')
  files=$(grep -lE 'singularity[^|]*exec|apptainer[^|]*exec' $cand 2>/dev/null)
  if [ -z "$files" ]; then
    echo "  ?? $name  (no command wrapper among $(printf '%s\n' $cand | wc -l) sampled"
    echo "            files -- raise SAMPLE, the wrappers may sort later)"
    continue
  fi
  hits=$(grep -lE -- "$RISKY" $files 2>/dev/null | head -5)
  if [ -n "$hits" ]; then
    found=1
    echo "  !! $name"
    for h in $hits; do
      echo "       $(basename "$h"): $(grep -oE -- "$RISKY[^ ]*" "$h" | sort -u | tr '\n' ' ')"
    done
  else
    n=$(printf '%s
' $files | wc -l)
    echo "  ok $name  ($n wrappers sampled, plain read-only exec)"
  fi
done

echo
if [ "$found" != 0 ]; then
  echo "A wrapper passes a privilege-changing option."
  echo
  echo "Under apptainer userns mode a plain read-only SIF exec succeeds and an"
  echo "--overlay invocation fails. If the pool runs userns, THAT TOOL breaks and"
  echo "the others do not -- which in a benchmark reads as that tool performing"
  echo "badly, not as infrastructure failing. Decide the privilege mode against"
  echo "this list, and re-run it after any Neurodesk image update."
  exit 1
fi
echo "Every wrapper checked is a plain read-only SIF exec. Userns mode is viable"
echo "for these tools. Re-run after any Neurodesk image update: this is a property"
echo "of the images, not of our code, and it can change without us doing anything."
exit 0
