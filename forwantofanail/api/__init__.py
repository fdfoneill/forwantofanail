"""HTTP API package.

Import the application explicitly from :mod:`forwantofanail.api.app`. Keeping
package initialization side-effect free also lets domain adapters reuse route
services without recursively constructing the ASGI application.
"""
