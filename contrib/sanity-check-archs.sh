#! /bin/sh
for i in `docker images --format {{.ID}}`; do echo $i `docker image inspect $i |grep Architecture`; done
