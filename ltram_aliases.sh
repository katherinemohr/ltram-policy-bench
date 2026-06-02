LTRAM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

rebuildroot() {
    cd $LTRAM_DIR/buildroot
    make -j8
    cd -
}
<<<<<<< HEAD

buildlinux() {
    cd $LTRAM_DIR/linux
    make -j8 bzImage
    cd -
}

alias vm="bash $LTRAM_DIR/scripts/run-vm.sh"
alias vm_i="vm interactive"
=======
>>>>>>> 79d0d13f182c1ee246d0b5152341a4ab7a2520c6
