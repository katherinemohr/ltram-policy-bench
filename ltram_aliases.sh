LTRAM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

rebuildroot() {
    cd $LTRAM_DIR/buildroot
    make -j8
    cd -
}
