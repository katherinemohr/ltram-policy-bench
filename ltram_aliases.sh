LTRAM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

rebuildroot() {
    cd $LTRAM_DIR/buildroot
    make -j8
    cd -
}

buildlinux() {
    cd $LTRAM_DIR/linux
    make -j8 bzImage
    cd -
}

alias vm="bash $LTRAM_DIR/scripts/run-vm.sh"
alias vm_i="vm interactive"
