#! /bin/sh
set -e

ruff check --fix .
ruff format .
djlint --reformat guncadmirror/templates/
./contrib/yamlfmt.sh
